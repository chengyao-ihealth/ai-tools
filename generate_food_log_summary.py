#!/usr/bin/env python3
"""
Generate Food Log Summary for a Specific Patient
为特定病人生成食物日志总结

This script reads food logs for a specific patient on a specific date,
downloads images, and generates an HTML summary similar to the attached image format.
这个脚本读取特定病人在特定日期的食物日志，下载图片，并生成类似附图的HTML总结。
"""
import argparse
import os
import sys
import html
import json
import base64
from datetime import datetime, timedelta
import pytz
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from collections import defaultdict

import pandas as pd
import httpx
from httpx import HTTPStatusError

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print("[WARN] OpenAI package not installed. Meal summary analysis will be disabled.", file=sys.stderr)
    print("[WARN] Install with: pip install openai", file=sys.stderr)

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ConfigurationError
    from bson import ObjectId
    from dotenv import load_dotenv
except ImportError:
    print("[ERROR] Missing required packages. Please install: pip install pymongo pandas httpx python-dotenv", file=sys.stderr)
    sys.exit(1)

# Load environment variables
load_dotenv()

# Import functions from existing modules
try:
    from query_food_logs import get_mongo_client, query_food_logs
except ImportError:
    print("[ERROR] Could not import from query_food_logs. Make sure query_food_logs.py is in the same directory.", file=sys.stderr)
    sys.exit(1)

try:
    from cache_db import CacheDB
except ImportError:
    CacheDB = None
    print("[WARN] CacheDB not available. Caching will be disabled.", file=sys.stderr)

# API base URL from environment variable
UC_BACKEND_API_BASE_URL = os.getenv("UC_BACKEND_API_BASE_URL", "https://uc-prod.ihealth-eng.com/v1/uc")
API_BASE = f"{UC_BACKEND_API_BASE_URL}/food-log"


# Helper functions from download_images.py (included here to avoid import issues)
def make_headers(session_token: str):
    """Generate request headers for API calls."""
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://ucfe-dev.ihealth-eng.com",
        "referer": "https://ucfe-dev.ihealth-eng.com/",
        "user-agent": "Mozilla/5.0",
        "x-session-token": session_token,
    }


def guess_ext_from_url(url: str) -> str:
    """Guess file extension from URL."""
    from pathlib import PurePosixPath
    parsed = PurePosixPath(url.split("?")[0])
    ext = parsed.suffix.lower()
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        return ext
    return ".jpg"


def extract_links(payload: dict, debug: bool = False):
    """Extract image links from API payload.
    Images are in data.images[].link (NOT fileKey)
    """
    if not payload or not isinstance(payload, dict):
        if debug:
            print(f"[DEBUG] extract_links: payload is not a dict or is None")
        return []
    
    # Get data.images structure
    data = payload.get("data", {})
    if not isinstance(data, dict):
        if debug:
            print(f"[DEBUG] extract_links: data is not a dict, type: {type(data)}")
        return []
    
    images = data.get("images", [])
    if debug:
        print(f"[DEBUG] extract_links: images type: {type(images)}, value: {images}")
    
    links = []
    if isinstance(images, list):
        if debug:
            print(f"[DEBUG] extract_links: Found {len(images)} image(s) in images array")
        for i, item in enumerate(images):
            if debug:
                print(f"[DEBUG] extract_links: Processing image {i+1}, type: {type(item)}")
            if isinstance(item, dict):
                if "link" in item:
                    link = item["link"]
                    if debug:
                        print(f"[DEBUG] extract_links: Image {i+1} - Found 'link' key, value: {link[:100] if isinstance(link, str) and len(link) > 100 else link}")
                    if link and isinstance(link, str):
                        links.append(link.strip() if link.strip() else link)
                    elif debug:
                        print(f"[DEBUG] extract_links: Image {i+1} - 'link' is empty or not a string")
                else:
                    if debug:
                        print(f"[DEBUG] extract_links: Image {i+1} - No 'link' key, available keys: {list(item.keys())}")
            elif isinstance(item, str):
                if debug:
                    print(f"[DEBUG] extract_links: Image {i+1} - Item is a string (direct link): {item[:100]}")
                links.append(item)
    elif isinstance(images, dict) and "link" in images:
        if debug:
            print(f"[DEBUG] extract_links: images is a dict with 'link' key")
        links.append(images["link"])
    elif isinstance(images, str):
        if debug:
            print(f"[DEBUG] extract_links: images is a string (direct link)")
        links.append(images)
    else:
        if debug:
            print(f"[DEBUG] extract_links: images is not a list/dict/string, cannot extract links")
    
    if debug:
        print(f"[DEBUG] extract_links: Returning {len(links)} link(s)")
    
    return links


def get_lab_results(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare") -> Dict[str, Any]:
    """
    Get lab results for a patient from MongoDB.
    从MongoDB获取患者的实验室检查结果。
    
    Args:
        client: MongoDB client
        patient_id: Patient ID (memberId in lab_results collection)
        database_name: Database name
        
    Returns:
        Dict with lab results
    """
    db = client[database_name]
    lab_results = {
        "glucose_fasting": None,
        "hba1c": None,
        "cholesterol_total": None,
        "ldl": None,
        "hdl": None,
        "triglycerides": None,
        "weight_lbs": None,
        "bmi": None,
        "blood_pressure": None,
        "last_updated": None,
    }
    
    try:
        # Try different collection names
        for collection_name in ["lab_results", "labResults", "lab_results_v2"]:
            try:
                collection = db[collection_name]
                
                # Try different ID fields
                for id_field in ["memberId", "member_id", "patientId", "patient_id", "_id"]:
                    try:
                        query_id = patient_id
                        if id_field == "_id" or id_field == "memberId":
                            try:
                                query_id = ObjectId(patient_id)
                            except Exception:
                                pass
                        
                        query = {id_field: query_id}
                        # Get most recent lab results (sorted by test_date descending)
                        docs = list(collection.find(query).sort("test_date", -1).limit(100))
                        
                        if docs:
                            for doc in docs:
                                test_date = doc.get("test_date")
                                if not test_date:
                                    continue
                                
                                test_type = (doc.get("test_type") or doc.get("testType") or "").lower()
                                test_value = doc.get("test_value") or doc.get("testValue")
                                
                                if not test_value:
                                    continue
                                
                                # Map lab types to our dict keys
                                if "glucose" in test_type and "fasting" in test_type:
                                    if lab_results["glucose_fasting"] is None:
                                        lab_results["glucose_fasting"] = test_value
                                elif "hba1c" in test_type or "a1c" in test_type:
                                    if lab_results["hba1c"] is None:
                                        lab_results["hba1c"] = test_value
                                elif "cholesterol" in test_type and "total" in test_type:
                                    if lab_results["cholesterol_total"] is None:
                                        lab_results["cholesterol_total"] = test_value
                                elif "ldl" in test_type:
                                    if lab_results["ldl"] is None:
                                        lab_results["ldl"] = test_value
                                elif "hdl" in test_type:
                                    if lab_results["hdl"] is None:
                                        lab_results["hdl"] = test_value
                                elif "triglyceride" in test_type:
                                    if lab_results["triglycerides"] is None:
                                        lab_results["triglycerides"] = test_value
                                
                                # Track the most recent update
                                if lab_results["last_updated"] is None:
                                    if isinstance(test_date, datetime):
                                        lab_results["last_updated"] = test_date.strftime("%Y-%m-%d")
                                    elif isinstance(test_date, str):
                                        lab_results["last_updated"] = test_date[:10]  # Take first 10 chars (YYYY-MM-DD)
                            
                            if any(v is not None for v in lab_results.values() if v != "last_updated"):
                                print(f"[INFO] Found lab results from {collection_name} collection")
                                return lab_results
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as e:
        print(f"[WARN] Error retrieving lab results: {e}")
    
    return lab_results


def get_glucose_data(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare", days: int = 30) -> Dict[str, Any]:
    """
    Get glucose/blood glucose measurement data for a patient from MongoDB.
    从MongoDB获取患者的血糖测量数据。
    
    Args:
        client: MongoDB client
        patient_id: Patient ID (memberId in measurements collection)
        database_name: Database name
        days: Number of days to look back (default 30)
        
    Returns:
        Dict with glucose data summary
    """
    db = client[database_name]
    
    try:
        # Try different collection names
        for collection_name in ["measurements", "Measurement"]:
            try:
                collection = db[collection_name]
                
                # Calculate date threshold
                from_date = datetime.now() - timedelta(days=days)
                
                # Try different ID fields
                for id_field in ["memberId", "member_id", "patientId", "patient_id"]:
                    try:
                        query_id = patient_id
                        if id_field == "memberId":
                            try:
                                query_id = ObjectId(patient_id)
                            except Exception:
                                pass
                        
                        # Query for blood glucose measurements
                        query = {
                            id_field: query_id,
                            "type": {"$in": ["BG", "blood_glucose", "Blood Glucose", "glucose"]},
                        }
                        
                        # Add date filter if available
                        docs = list(collection.find(query).sort("date", -1).limit(100))
                        
                        if docs:
                            values = []
                            for doc in docs:
                                # Extract value
                                value = doc.get("value") or doc.get("Value")
                                if value:
                                    try:
                                        values.append(float(value))
                                    except (ValueError, TypeError):
                                        continue
                            
                            if values:
                                avg_glucose = sum(values) / len(values)
                                min_glucose = min(values)
                                max_glucose = max(values)
                                
                                print(f"[INFO] Found {len(values)} glucose measurements from {collection_name} collection")
                                
                                return {
                                    "has_data": True,
                                    "count": len(values),
                                    "average": round(avg_glucose, 1),
                                    "min": round(min_glucose, 1),
                                    "max": round(max_glucose, 1),
                                }
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as e:
        print(f"[WARN] Error retrieving glucose data: {e}")
    
    return {"has_data": False, "count": 0}


# Constants for unit conversion (matching ai-agent-service)
LBS_TO_KG_RATIO = 0.453592
INCH_TO_CM_RATIO = 2.54

def race_mapping():
    """Mapping of race codes to display names (matching ai-agent-service)"""
    return {
        'WHITE': 'White',
        'BAA': 'African American',
        'ASIAN': 'Asian',
        'HISPANIC': 'Hispanic',
        'OTHER': '',
        'NHOPI': 'Native Hawaiian or Other Pacific Islander',
        'NOT_TO_SAY': '',
        'AIAN': 'American Indian or Alaska Native',
        'MULTIRACIAL': 'Multiracial'
    }

def diagnoses_mapping():
    """Mapping of diagnosis codes to display names (matching ai-agent-service)"""
    return {
        'CHF': 'CHF',
        'COPD': 'COPD',
        'DM': 'DM',
        'DM2': 'DM2',
        'DM_OTHERS': 'DM Others',
        'CKD': 'CKD',
        'CKF': 'CKD',
        'RDD': 'RDD',
        'HYPOTENSION': 'Hypotension',
        'ESRD_Dialysis': 'ESRD Dialysis',
        'HLD': 'HLD',
        'HTN': 'HTN',
        'OBESITY': 'Obesity',
        'PRE_DM': 'PreDM',
    }

def gender_mapping():
    """Mapping of gender codes to display names"""
    return {'F': 'Female', 'M': 'Male'}

def calculate_age_from_birthday(birthday: str) -> Optional[int]:
    """Calculate age from birthday string (format: 'YYYY-MM-DD')"""
    if not birthday:
        return None
    try:
        birth_date = datetime.strptime(birthday, '%Y-%m-%d')
        today = datetime.today()
        age = today.year - birth_date.year - (
            (today.month, today.day) < (birth_date.month, birth_date.day)
        )
        return age
    except (ValueError, TypeError):
        return None

def convert_weight_to_kg(weight_value: Optional[float], unit: Optional[str]) -> Optional[str]:
    """Convert weight to kg if unit is lb/lbs, otherwise return as kg. Returns string with no decimals."""
    if weight_value is None:
        return None
    try:
        weight_kg = float(weight_value)
        if unit and unit.lower() in ("lb", "lbs"):
            weight_kg *= LBS_TO_KG_RATIO
        return format(weight_kg, ".0f")
    except (ValueError, TypeError):
        return None

def convert_height_to_cm(height_value: Optional[float], unit: Optional[str]) -> Optional[str]:
    """Convert height to cm if unit is inch, otherwise assume cm. Returns string with no decimals."""
    if height_value is None:
        return None
    try:
        height_cm = float(height_value)
        if unit and unit.lower() in ("inch", "inches"):
            height_cm *= INCH_TO_CM_RATIO
        return format(height_cm, ".0f")
    except (ValueError, TypeError):
        return None

def get_nutrition_info_with_ai(
    client: MongoClient, 
    patient_id: str, 
    database_name: str = "UnifiedCare",
    openai_api_key: Optional[str] = None,
    cache_db: Optional[CacheDB] = None,
    debug: bool = False
) -> Dict[str, Any]:
    """
    Get nutrition-related information using AI to summarize from multiple data sources.
    使用AI从多个数据源总结获取营养相关信息。
    
    Data sources: care notes, assessments, sticky notes, chat message history
    数据源：care notes, assessments, sticky notes, chat message history
    
    Returns:
        Dict with:
        - nutrition_diagnoses: Nutrition-related diagnoses (AI summarized)
        - biggest_medical_problem: Biggest medical problem (AI summarized)
        - nutrition_behavioral_goals: Nutrition behavioral goals (AI summarized)
        - nutrition_assessment: Initial nutrition assessment (AI summarized)
    """
    # First collect all raw data
    # 首先收集所有原始数据
    raw_data = collect_patient_data_sources(client, patient_id, database_name, debug=debug)
    
    # Use AI to summarize
    # 使用AI总结
    if OpenAI is None:
        print("[WARN] OpenAI not available, falling back to basic extraction")
        return get_nutrition_info(client, patient_id, database_name)
    
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[WARN] OPENAI_API_KEY not found, falling back to basic extraction")
        return get_nutrition_info(client, patient_id, database_name)
    
    # Check cache (use food_log_id=None, image_url=None as cache key)
    # 检查缓存（使用food_log_id=None, image_url=None作为缓存键）
    if cache_db:
        # Use patient_id as a unique identifier for caching
        # 使用patient_id作为缓存的唯一标识符
        cached = cache_db.get_ai_summary_cache(food_log_id=f"nutrition_info_{patient_id}", image_url=None, patient_notes=None)
        if cached:
            if debug:
                print(f"[DEBUG] Using cached AI nutrition info summary")
            return cached
    
    # Generate AI summary
    # 生成AI总结
    try:
        summary = generate_nutrition_info_ai_summary(raw_data, api_key, debug=debug)
        
        # Save to cache
        # 保存到缓存
        if cache_db and summary:
            cache_db.save_ai_summary_cache(summary, food_log_id=f"nutrition_info_{patient_id}", image_url=None, patient_notes=None)
        
        return summary
    except Exception as e:
        print(f"[WARN] AI summary failed: {e}, falling back to basic extraction")
        if debug:
            import traceback
            traceback.print_exc()
        return get_nutrition_info(client, patient_id, database_name)


def collect_patient_data_sources(
    client: MongoClient, 
    patient_id: str, 
    database_name: str = "UnifiedCare",
    debug: bool = False
) -> Dict[str, Any]:
    """
    Collect all relevant data sources for AI summarization.
    收集所有相关数据源用于AI总结。
    
    Returns:
        Dict with collected data from:
        - care_notes: All care notes
        - assessments: Nutrition assessments
        - sticky_notes: Sticky notes
        - chat_messages: Chat message history
        - patient_info: Basic patient info
    """
    db = client[database_name]
    result = {
        "care_notes": [],
        "assessments": [],
        "sticky_notes": [],
        "chat_messages": [],
        "patient_info": {}
    }
    
    try:
        query_id = patient_id
        try:
            query_id = ObjectId(patient_id)
        except Exception:
            pass
        
        # 1. Get care notes
        # 获取care notes
        try:
            care_notes_collection = db["uc_care_notes"]
            care_notes = list(care_notes_collection.find({
                "$or": [
                    {"memberId": query_id},
                    {"memberId": patient_id},
                    {"patient_id": query_id},
                    {"patient_id": patient_id}
                ]
            }).sort("createdAt", -1).limit(100))
            
            for note in care_notes:
                note_text = ""
                for field in ["note", "content", "text", "assessment"]:
                    if note.get(field):
                        note_text = str(note[field])
                        break
                if note_text:
                    result["care_notes"].append({
                        "text": note_text,
                        "createdAt": str(note.get("createdAt", ""))
                    })
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error getting care notes: {e}")
        
        # 2. Get assessments (from uc_nutritions, uc_monthly_review)
        # 获取评估（从uc_nutritions, uc_monthly_review）
        try:
            # uc_nutritions
            nutritions_collection = db["uc_nutritions"]
            nutrition_docs = list(nutritions_collection.find({
                "$or": [
                    {"memberId": query_id},
                    {"memberId": patient_id},
                    {"patient_id": query_id},
                    {"patient_id": patient_id}
                ]
            }).sort("createdAt", -1).limit(10))
            
            for doc in nutrition_docs:
                for key in ["assessment", "initialAssessment", "baselineAssessment", "summary", "notes"]:
                    value = doc.get(key)
                    if value:
                        result["assessments"].append({
                            "text": str(value),
                            "type": "nutrition",
                            "createdAt": str(doc.get("createdAt", ""))
                        })
                        break
            
            # uc_monthly_review
            monthly_review_collection = db["uc_monthly_review"]
            reviews = list(monthly_review_collection.find({
                "$or": [
                    {"memberId": query_id},
                    {"memberId": patient_id},
                    {"patient_id": query_id},
                    {"patient_id": patient_id}
                ]
            }).sort("createdAt", 1).limit(10))  # Earliest first for initial assessment
            
            for review in reviews:
                for key in ["assessment", "nutritionAssessment", "initialAssessment", "summary", "notes"]:
                    value = review.get(key)
                    if value:
                        result["assessments"].append({
                            "text": str(value),
                            "type": "monthly_review",
                            "createdAt": str(review.get("createdAt", ""))
                        })
                        break
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error getting assessments: {e}")
        
        # 3. Get sticky notes (if collection exists)
        # 获取sticky notes（如果集合存在）
        try:
            sticky_notes_collection = db.get_collection("uc_sticky_notes")
            if sticky_notes_collection:
                sticky_notes = list(sticky_notes_collection.find({
                    "$or": [
                        {"memberId": query_id},
                        {"memberId": patient_id},
                        {"patient_id": query_id},
                        {"patient_id": patient_id}
                    ]
                }).sort("createdAt", -1).limit(50))
                
                for note in sticky_notes:
                    note_text = ""
                    for field in ["note", "content", "text", "message"]:
                        if note.get(field):
                            note_text = str(note[field])
                            break
                    if note_text:
                        result["sticky_notes"].append({
                            "text": note_text,
                            "createdAt": str(note.get("createdAt", ""))
                        })
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error getting sticky notes: {e}")
        
        # 4. Get chat messages (if collection exists)
        # 获取chat messages（如果集合存在）
        try:
            # Try different possible collection names
            # 尝试不同的可能集合名称
            for collection_name in ["uc_chat_messages", "uc_chat", "chat_messages", "messages"]:
                try:
                    chat_collection = db[collection_name]
                    chat_messages = list(chat_collection.find({
                        "$or": [
                            {"memberId": query_id},
                            {"memberId": patient_id},
                            {"patientId": query_id},
                            {"patientId": patient_id},
                            {"patient_id": query_id},
                            {"patient_id": patient_id}
                        ]
                    }).sort("createdAt", -1).limit(100))
                    
                    for msg in chat_messages:
                        msg_text = ""
                        for field in ["message", "content", "text", "body"]:
                            if msg.get(field):
                                msg_text = str(msg[field])
                                break
                        if msg_text:
                            result["chat_messages"].append({
                                "text": msg_text,
                                "createdAt": str(msg.get("createdAt", "")),
                                "sender": str(msg.get("sender", ""))
                            })
                    
                    if result["chat_messages"]:
                        break  # Found messages, stop trying other collections
                except Exception:
                    continue
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error getting chat messages: {e}")
        
        # 5. Get basic patient info
        # 获取基本患者信息
        try:
            patients_collection = db["uc_patients"]
            patient_doc = patients_collection.find_one({"_id": query_id}) or patients_collection.find_one({"_id": patient_id})
            if patient_doc:
                result["patient_info"] = {
                    "diagnoses": patient_doc.get("diagnoses", []),
                    "healthConditions": patient_doc.get("healthConditions", [])
                }
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error getting patient info: {e}")
    
    except Exception as e:
        if debug:
            print(f"[DEBUG] Error collecting data sources: {e}")
    
    return result


def generate_nutrition_info_ai_summary(
    raw_data: Dict[str, Any],
    openai_api_key: str,
    debug: bool = False
) -> Dict[str, Any]:
    """
    Use AI to summarize nutrition information from collected data sources.
    使用AI从收集的数据源总结营养信息。
    """
    if OpenAI is None:
        return {}
    
    client = OpenAI(api_key=openai_api_key)
    
    # Build prompt with all collected data
    # 构建包含所有收集数据的prompt
    prompt = f"""You are a clinical nutritionist analyzing patient information from multiple data sources.

PATIENT DATA SOURCES:
1. Care Notes ({len(raw_data.get('care_notes', []))} notes):
{chr(10).join([f"- {note.get('text', '')[:500]}" for note in raw_data.get('care_notes', [])[:20]])}

2. Assessments ({len(raw_data.get('assessments', []))} assessments):
{chr(10).join([f"- [{note.get('type', 'unknown')}] {note.get('text', '')[:500]}" for note in raw_data.get('assessments', [])[:10]])}

3. Sticky Notes ({len(raw_data.get('sticky_notes', []))} notes):
{chr(10).join([f"- {note.get('text', '')[:500]}" for note in raw_data.get('sticky_notes', [])[:20]])}

4. Chat Messages ({len(raw_data.get('chat_messages', []))} messages):
{chr(10).join([f"- {note.get('text', '')[:500]}" for note in raw_data.get('chat_messages', [])[:30]])}

5. Patient Diagnoses and Health Conditions:
- Diagnoses: {raw_data.get('patient_info', {}).get('diagnoses', [])}
- Health Conditions: {raw_data.get('patient_info', {}).get('healthConditions', [])}

Based on the above information, please provide a concise, structured summary for each of the following 4 items:

1. **Nutrition-Related Diagnoses**: List nutrition-related diagnoses (e.g., diabetes, hypertension, hyperlipidemia, obesity, kidney disease, etc.). Use bullet points or comma-separated list. Be specific and clear.

2. **Primary Medical Problem**: Identify the patient's biggest/primary medical problem currently. State it clearly with brief context if needed.

3. **Nutrition Behavioral Goals**: Extract and list all nutrition behavioral goals from care notes, assessments, sticky notes, and chat messages. Format as structured list with:
   - Goal name/description
   - Target value (if mentioned)
   - Current status/progress (if mentioned)
   Use bullet points or structured format. Be concise but clear.

4. **Initial Nutrition Assessment**: Summarize key findings from the initial nutrition assessment. Use bullet points for key points. Focus on actionable insights and important observations.

Please format your response as JSON with the following structure:
{{
    "nutrition_diagnoses": "concise text (bullet points or comma-separated)",
    "biggest_medical_problem": "concise text with clear statement",
    "nutrition_behavioral_goals": "structured text (bullet points or structured format)",
    "nutrition_assessment": "concise text (bullet points for key findings)"
}}

Guidelines:
- Use bullet points (•) or structured format for clarity
- Be concise - no need for complete sentences, but reasoning must be clear
- Focus on facts and actionable information
- Remove redundant information
- Keep each item under 200 words
"""
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a clinical nutritionist. Provide concise, structured summaries using bullet points and clear formatting. No need for complete sentences - use brief phrases and structured lists. Be clear and factual."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1200
        )
        
        content = response.choices[0].message.content
        
        # Try to parse JSON response
        # 尝试解析JSON响应
        import json
        # Remove markdown code blocks if present
        # 如果存在，移除markdown代码块
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        try:
            summary = json.loads(content)
        except json.JSONDecodeError as e:
            if debug:
                print(f"[DEBUG] JSON parse error: {e}")
                print(f"[DEBUG] Content that failed to parse (first 1000 chars): {content[:1000]}")
            # Try to extract JSON from the text if it's embedded
            # 如果JSON嵌入在文本中，尝试提取
            import re
            # Try to find JSON object in the content
            # 尝试在内容中找到JSON对象
            json_match = re.search(r'\{[^{}]*(?:"nutrition_diagnoses"|"biggest_medical_problem"|"nutrition_behavioral_goals"|"nutrition_assessment")[^{}]*\}', content, re.DOTALL)
            if json_match:
                try:
                    summary = json.loads(json_match.group(0))
                except:
                    if debug:
                        print(f"[DEBUG] Failed to parse extracted JSON")
                    # Fallback: return empty dict, will use basic extraction
                    # 回退：返回空字典，将使用基本提取
                    return {}
            else:
                if debug:
                    print(f"[DEBUG] No JSON pattern found in content")
                return {}
        
        # Convert values to strings, handling lists and dicts
        # 将值转换为字符串，处理列表和字典
        def format_value(value):
            if value is None:
                return None
            if isinstance(value, str):
                # If string looks like JSON (starts with [ or {), try to parse it
                # 如果字符串看起来像JSON（以[或{开头），尝试解析它
                value_stripped = value.strip()
                if (value_stripped.startswith('[') or value_stripped.startswith('{')) and not value_stripped.startswith('•'):
                    try:
                        parsed = json.loads(value_stripped)
                        return format_value(parsed)  # Recursively format
                    except:
                        pass  # Not valid JSON, return as string
                return value
            elif isinstance(value, list):
                # Convert list to readable format
                # 将列表转换为可读格式
                formatted_items = []
                for item in value:
                    if isinstance(item, dict):
                        # Format dict items as structured text
                        # 将字典项格式化为结构化文本
                        goal_name = item.get('goal', item.get('name', ''))
                        target = item.get('target_value', item.get('target', ''))
                        status = item.get('current_status', item.get('status', ''))
                        
                        if goal_name:
                            parts = [goal_name]
                            if target:
                                parts.append(f"Target: {target}")
                            if status:
                                parts.append(f"Status: {status}")
                            formatted_items.append("; ".join(parts))
                        else:
                            # Fallback: format all key-value pairs
                            # 回退：格式化所有键值对
                            parts = []
                            for k, v in item.items():
                                if k not in ['goal', 'name', 'target_value', 'target', 'current_status', 'status']:
                                    parts.append(f"{k}: {v}")
                            if parts:
                                formatted_items.append("; ".join(parts))
                    else:
                        formatted_items.append(str(item))
                return "\n".join([f"• {item}" for item in formatted_items if item])
            elif isinstance(value, dict):
                # Convert dict to readable format
                # 将字典转换为可读格式
                parts = []
                for k, v in value.items():
                    parts.append(f"{k}: {v}")
                return "; ".join(parts)
            else:
                return str(value)
        
        return {
            "nutrition_diagnoses": format_value(summary.get("nutrition_diagnoses")),
            "biggest_medical_problem": format_value(summary.get("biggest_medical_problem")),
            "nutrition_behavioral_goals": format_value(summary.get("nutrition_behavioral_goals")),
            "nutrition_assessment": format_value(summary.get("nutrition_assessment"))
        }
    except Exception as e:
        if debug:
            print(f"[DEBUG] AI summary generation failed: {e}")
            import traceback
            traceback.print_exc()
        return {}


def get_nutrition_info(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare") -> Dict[str, Any]:
    """
    Get nutrition-related information for daily summary display (basic extraction without AI).
    获取用于每日总结显示的营养相关信息（基本提取，不使用AI）。
    
    Returns:
        Dict with:
        - nutrition_diagnoses: Nutrition-related diagnoses
        - biggest_medical_problem: Biggest medical problem
        - nutrition_behavioral_goals: Nutrition behavioral goals from care notes
        - nutrition_assessment: Initial nutrition assessment
    """
    db = client[database_name]
    result = {
        "nutrition_diagnoses": None,
        "biggest_medical_problem": None,
        "nutrition_behavioral_goals": None,
        "nutrition_assessment": None
    }
    
    try:
        # Convert patient_id to ObjectId if valid
        query_id = patient_id
        try:
            query_id = ObjectId(patient_id)
        except Exception:
            pass
        
        # 1. Get nutrition-related diagnoses and biggest medical problem from uc_patients
        patients_collection = db["uc_patients"]
        patient_doc = patients_collection.find_one({"_id": query_id}) or patients_collection.find_one({"_id": patient_id})
        
        if patient_doc:
            # Get all diagnoses
            diagnoses = patient_doc.get("diagnoses", [])
            health_conditions = patient_doc.get("healthConditions", [])
            
            # Nutrition-related keywords (including common medical abbreviations)
            # 营养相关关键词（包括常见医学缩写）
            nutrition_keywords = [
                # Diabetes / 糖尿病
                "diabetes", "diabetic", "prediabetes", "dm", "dm1", "dm2", "t1d", "t2d",
                # Obesity / 肥胖
                "obesity", "overweight", "bmi",
                # Hypertension / 高血压
                "hypertension", "htn", "high blood pressure", "hbp",
                # Hyperlipidemia / 高血脂
                "hyperlipidemia", "cholesterol", "hld", "dyslipidemia", "high cholesterol",
                # Cardiovascular / 心血管
                "cvd", "cardiovascular", "heart disease", "cad", "coronary",
                # Kidney / 肾脏
                "kidney", "renal", "ckd", "chronic kidney", "nephropathy",
                # Nutrition / 营养
                "nutrition", "diet", "dietary", "metabolic", "metabolism"
            ]
            
            # Filter nutrition-related diagnoses
            nutrition_diag_list = []
            if diagnoses:
                diag_mapping = diagnoses_mapping()
                for diag in diagnoses:
                    if diag:
                        mapped_diag = diag_mapping.get(diag, diag)
                        diag_lower = str(mapped_diag).lower()
                        if any(keyword in diag_lower for keyword in nutrition_keywords):
                            nutrition_diag_list.append(mapped_diag)
            
            if nutrition_diag_list:
                result["nutrition_diagnoses"] = ", ".join(nutrition_diag_list)
            
            # Get all medical problems (from healthConditions and diagnoses)
            # 获取所有医疗问题（从healthConditions和diagnoses）
            all_problems = []
            
            # Add health conditions first
            # 先添加health conditions
            if health_conditions:
                for item in health_conditions:
                    if isinstance(item, dict):
                        condition = item.get("condition")
                        if condition:
                            condition_str = str(condition).strip()
                            if condition_str and condition_str not in all_problems:
                                all_problems.append(condition_str)
                    elif isinstance(item, str):
                        item_str = item.strip()
                        if item_str and item_str not in all_problems:
                            all_problems.append(item_str)
            
            # Add diagnoses (avoid duplicates)
            # 添加诊断（避免重复）
            if diagnoses:
                diag_mapping = diagnoses_mapping()
                for diag in diagnoses:
                    if diag:
                        mapped_diag = diag_mapping.get(diag, diag)
                        mapped_diag_str = str(mapped_diag).strip()
                        if mapped_diag_str and mapped_diag_str not in all_problems:
                            all_problems.append(mapped_diag_str)
            
            # Join all problems with comma
            # 用逗号连接所有问题
            if all_problems:
                result["biggest_medical_problem"] = ", ".join(all_problems)
        
        # 2. Get nutrition behavioral goals from uc_behavior_goals or care notes
        # uc_behavior_goals: document _id IS patient ID (same as agent-service BehaviorGoal.get(patient_id))
        try:
            behavior_goals_collection = db["uc_behavior_goals"]
            goals_doc = behavior_goals_collection.find_one({"_id": query_id})
            if not goals_doc and query_id != patient_id:
                goals_doc = behavior_goals_collection.find_one({"_id": patient_id})
            if not goals_doc:
                goals_doc = behavior_goals_collection.find_one({"memberId": query_id}) or \
                           behavior_goals_collection.find_one({"memberId": patient_id}) or \
                           behavior_goals_collection.find_one({"patient_id": query_id}) or \
                           behavior_goals_collection.find_one({"patient_id": patient_id})
            if goals_doc:
                goals_text = []
                # Prefer behaviorGoals list (BehaviorGoalItem: behaviorGoalType, goalStatus, behaviorGoalValue)
                behavior_goals_list = goals_doc.get("behaviorGoals") or goals_doc.get("behavior_goals")
                if behavior_goals_list and isinstance(behavior_goals_list, list):
                    for item in behavior_goals_list[:10]:
                        if isinstance(item, dict):
                            v = item.get("behaviorGoalValue") or item.get("behavior_goal_value") or item.get("value")
                            if v and str(v).strip():
                                goals_text.append(str(v).strip())
                        else:
                            goals_text.append(str(item))
                if not goals_text:
                    for key in ["goal", "goals", "nutritionGoal", "nutrition_goal", "behavioralGoal", "behavioral_goal"]:
                        value = goals_doc.get(key)
                        if value:
                            if isinstance(value, list):
                                goals_text.extend([str(g) for g in value])
                            else:
                                goals_text.append(str(value))
                if goals_text:
                    result["nutrition_behavioral_goals"] = "; ".join(goals_text[:10])
        except Exception as e:
            print(f"[DEBUG] Error querying uc_behavior_goals: {e}")
        
        # If no goals from uc_behavior_goals, try care notes
        if not result["nutrition_behavioral_goals"]:
            try:
                care_notes_collection = db["uc_care_notes"]
                # Get all care notes for this patient
                notes = list(care_notes_collection.find({
                    "$or": [
                        {"memberId": query_id},
                        {"memberId": patient_id},
                        {"patient_id": query_id},
                        {"patient_id": patient_id}
                    ]
                }).sort("createdAt", -1).limit(50))
                
                # Search for nutrition behavioral goals in notes
                goal_keywords = ["nutrition", "diet", "eating", "food", "meal", "behavior", "goal"]
                goal_texts = []
                
                for note in notes:
                    note_text = ""
                    for field in ["note", "content", "text", "assessment"]:
                        if note.get(field):
                            note_text = str(note[field]).lower()
                            break
                    
                    if note_text and any(keyword in note_text for keyword in goal_keywords):
                        # Extract relevant sentences
                        sentences = note_text.split(".")
                        for sent in sentences:
                            if any(keyword in sent for keyword in goal_keywords):
                                goal_texts.append(sent.strip()[:100])  # Limit length
                                if len(goal_texts) >= 3:
                                    break
                    
                    if len(goal_texts) >= 3:
                        break
                
                if goal_texts:
                    result["nutrition_behavioral_goals"] = "; ".join(goal_texts)
            except Exception as e:
                print(f"[DEBUG] Error querying care notes for goals: {e}")
        
        # 3. Get initial nutrition assessment from uc_nutritions or uc_monthly_review
        # Try uc_nutritions first
        try:
            nutritions_collection = db["uc_nutritions"]
            nutrition_doc = nutritions_collection.find_one({"memberId": query_id}) or \
                          nutritions_collection.find_one({"memberId": patient_id}) or \
                          nutritions_collection.find_one({"patient_id": query_id}) or \
                          nutritions_collection.find_one({"patient_id": patient_id})
            
            if nutrition_doc:
                # Extract assessment
                for key in ["assessment", "initialAssessment", "baselineAssessment", "summary", "notes"]:
                    value = nutrition_doc.get(key)
                    if value:
                        result["nutrition_assessment"] = str(value)[:300]  # Limit length
                        break
        except Exception as e:
            print(f"[DEBUG] Error querying uc_nutritions: {e}")
        
        # If no assessment from uc_nutritions, try uc_monthly_review
        if not result["nutrition_assessment"]:
            try:
                monthly_review_collection = db["uc_monthly_review"]
                # Get earliest review (initial assessment)
                reviews = list(monthly_review_collection.find({
                    "$or": [
                        {"memberId": query_id},
                        {"memberId": patient_id},
                        {"patient_id": query_id},
                        {"patient_id": patient_id}
                    ]
                }).sort("createdAt", 1).limit(1))  # Earliest first
                
                if reviews:
                    review = reviews[0]
                    for key in ["assessment", "nutritionAssessment", "initialAssessment", "summary", "notes"]:
                        value = review.get(key)
                        if value:
                            result["nutrition_assessment"] = str(value)[:300]
                            break
            except Exception as e:
                print(f"[DEBUG] Error querying uc_monthly_review: {e}")
    
    except Exception as e:
        print(f"[WARN] Error getting nutrition info: {e}")
    
    return result


def format_text_with_line_breaks(text: str) -> str:
    """
    Format text with proper line breaks for better readability.
    格式化文本，使用适当的换行以提高可读性。
    
    Handles bullet points, double bullets, and ensures proper spacing.
    处理要点、双要点，并确保适当的间距。
    """
    if not text or not text.strip():
        return ""
    
    text = text.strip()
    
    # Replace double bullets with single / 将双要点替换为单要点
    text = text.replace('• •', '•')
    text = text.replace('•\n•', '•')
    
    # Split by bullet points / 按要点分割
    if '•' in text:
        parts = text.split('•')
        formatted_parts = []
        for part in parts:
            part = part.strip()
            if part:
                # Remove leading/trailing spaces and format
                # 移除前导/尾随空格并格式化
                formatted_parts.append(f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">• {html.escape(part)}</div>')
        if formatted_parts:
            return ''.join(formatted_parts)
    
    # If no bullets, check for other separators / 如果没有要点，检查其他分隔符
    # Check for patterns like "• " at start of lines
    # 检查行首的 "• " 模式
    lines = text.split('\n')
    formatted_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Check if line starts with bullet or is a list item
        # 检查行是否以要点开头或是列表项
        if line.startswith('•'):
            formatted_lines.append(f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">{html.escape(line)}</div>')
        elif line.startswith('-'):
            formatted_lines.append(f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">{html.escape(line)}</div>')
        else:
            formatted_lines.append(f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">{html.escape(line)}</div>')
    
    if formatted_lines:
        return ''.join(formatted_lines)
    
    # Fallback: just escape and preserve line breaks / 回退：仅转义并保留换行
    return html.escape(text).replace('\n', '<br>')


def format_nutrition_goals(goals_text: str, language: str = 'zh') -> str:
    """
    Format nutrition behavioral goals text into a structured, readable format.
    将营养行为目标文本格式化为结构化、易读的格式。
    
    Args:
        goals_text: Raw goals text / 原始目标文本
        language: Language code / 语言代码
        
    Returns:
        Formatted HTML string / 格式化的HTML字符串
    """
    if not goals_text or not goals_text.strip():
        return ""
    
    import re
    
    # Clean up the text - remove existing bullet points to avoid double bullets
    # 清理文本 - 移除现有的要点以避免双要点
    text = goals_text.strip()
    
    # Remove all existing bullet points (•, -, *, etc.) and clean up
    # 移除所有现有的要点（•, -, *, 等）并清理
    text = re.sub(r'^[\s•\-\*]+\s*', '', text, flags=re.MULTILINE)  # Remove leading bullets
    text = re.sub(r'\s*[\s•\-\*]+\s*', ' ', text)  # Replace multiple bullets/spaces with single space
    
    # Split by bullet points, semicolons, or newlines
    # 按要点、分号或换行符分割
    parts = []
    
    # First check if text contains bullet points (already formatted by AI)
    # 首先检查文本是否包含要点（已由AI格式化）
    if '•' in text:
        # Split by bullet points
        parts = [p.strip() for p in text.split('•') if p.strip()]
    elif ';' in text:
        # Split by semicolons
        parts = [p.strip() for p in text.split(';') if p.strip()]
    elif '\n' in text:
        # Split by newlines
        parts = [p.strip() for p in text.split('\n') if p.strip()]
    else:
        parts = [text]
    
    formatted_parts = []
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Remove any remaining leading/trailing bullets or spaces
        # 移除任何剩余的前导/尾随要点或空格
        part = re.sub(r'^[\s•\-\*]+', '', part).strip()
        part = re.sub(r'[\s•\-\*]+$', '', part).strip()
        
        if not part:
            continue
        
        # Format based on content
        # 根据内容格式化
        if ':' in part:
            # Has label:value format
            # 有标签:值格式
            colon_parts = part.split(':', 1)
            if len(colon_parts) == 2:
                label = colon_parts[0].strip()
                value = colon_parts[1].strip()
                formatted_parts.append(
                    f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">'
                    f'• <strong>{html.escape(label)}:</strong> {html.escape(value)}</div>'
                )
            else:
                formatted_parts.append(
                    f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">• {html.escape(part)}</div>'
                )
        else:
            # Simple text
            # 简单文本
            formatted_parts.append(
                f'<div style="margin: 4px 0; padding-left: 12px; line-height: 1.6;">• {html.escape(part)}</div>'
            )
    
    if formatted_parts:
        return '<div style="margin-top: 4px;">' + ''.join(formatted_parts) + '</div>'
    else:
        # Fallback: use format_text_with_line_breaks
        # 回退：使用 format_text_with_line_breaks
        return format_text_with_line_breaks(goals_text)


def get_patient_info(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare") -> Dict[str, Any]:
    """
    Get patient background information from database using the same structure as ai-agent-service.
    从数据库获取患者背景信息，使用与ai-agent-service相同的结构。
    
    This function queries the 'uc_patients' collection and extracts information from the 'profile' subdocument,
    matching the structure used in the ai-agent-service codebase.
    此函数查询'uc_patients'集合并从'profile'子文档中提取信息，匹配ai-agent-service代码库中使用的结构。
    
    Args:
        client: MongoDB client
        patient_id: Patient ID
        database_name: Database name
        
    Returns:
        Dict with patient information (always includes patient_id)
    """
    db = client[database_name]
    patient_info = {"patient_id": patient_id}  # Always include patient_id
    
    # Query uc_patients collection (matching ai-agent-service)
    collection = db["uc_patients"]
    
    try:
        # Try to convert patient_id to ObjectId
        query_id = patient_id
        try:
            query_id = ObjectId(patient_id)
        except Exception:
            pass
        
        # Query by _id
        doc = collection.find_one({"_id": query_id})
        
        if not doc:
            # Try as string if ObjectId failed
            doc = collection.find_one({"_id": patient_id})
        
        if doc:
            print(f"[INFO] Found document in uc_patients collection")
            print(f"[DEBUG] Document top-level keys: {list(doc.keys())[:20]}...")
            
            # Extract from profile subdocument (matching ai-agent-service structure)
            profile = doc.get("profile") or {}
            if profile:
                print(f"[DEBUG] Found profile subdocument with keys: {list(profile.keys())[:20]}...")
                
                # Name - from profile.firstName and profile.lastName
                first_name = profile.get("firstName") or ""
                last_name = profile.get("lastName") or ""
                if first_name or last_name:
                    patient_info["name"] = f"{first_name} {last_name}".strip()
                
                # Age - calculate from profile.birthday
                birthday = profile.get("birthday")
                if birthday:
                    age = calculate_age_from_birthday(birthday)
                    if age is not None:
                        patient_info["age"] = age
                
                # Gender - from profile.gender
                gender = profile.get("gender")
                if gender:
                    patient_info["gender"] = gender
                    # Also add gender_display
                    gender_display = gender_mapping().get(gender, gender)
                    if gender_display:
                        patient_info["gender_display"] = gender_display
                
                # Gender Identity - from profile.genderidentity
                gender_identity = profile.get("genderidentity")
                if gender_identity:
                    patient_info["gender_identity"] = str(gender_identity)
                
                # Weight - from profile.weight (object with value, unit, bmi)
                weight_obj = profile.get("weight")
                if weight_obj:
                    weight_value = weight_obj.get("value") if isinstance(weight_obj, dict) else None
                    weight_unit = weight_obj.get("unit") if isinstance(weight_obj, dict) else None
                    weight_kg = convert_weight_to_kg(weight_value, weight_unit)
                    if weight_kg:
                        patient_info["weight"] = weight_kg
                
                # Height - from profile.height (object with value, unit)
                height_obj = profile.get("height")
                if height_obj:
                    height_value = height_obj.get("value") if isinstance(height_obj, dict) else None
                    height_unit = height_obj.get("unit") if isinstance(height_obj, dict) else None
                    height_cm = convert_height_to_cm(height_value, height_unit)
                    if height_cm:
                        patient_info["height"] = height_cm
                
                # BMI - from profile.weight.bmi
                if weight_obj and isinstance(weight_obj, dict):
                    bmi = weight_obj.get("bmi")
                    if bmi is not None:
                        try:
                            patient_info["bmi"] = format(float(bmi), '.1f')
                        except (ValueError, TypeError):
                            pass
                
                # Race - from profile.race, map to race_display
                race = profile.get("race")
                if race:
                    race_display = race_mapping().get(race, race)
                    if race_display:
                        patient_info["ethnicity"] = race_display
                    patient_info["race"] = race
                
                # Nickname - from profile.nickName
                nickname = profile.get("nickName")
                if nickname:
                    patient_info["nickname"] = str(nickname)
            
            # Health Conditions - from top-level healthConditions (list of objects)
            health_conditions = doc.get("healthConditions")
            if health_conditions and isinstance(health_conditions, list):
                conditions_list = []
                for item in health_conditions:
                    if isinstance(item, dict):
                        condition = item.get("condition")
                        if condition:
                            conditions_list.append(str(condition))
                    elif isinstance(item, str):
                        conditions_list.append(item)
                if conditions_list:
                    patient_info["medical_history"] = ", ".join(conditions_list)
            
            # Diagnoses - from top-level diagnoses (list), map to diagnoses_display
            diagnoses = doc.get("diagnoses")
            if diagnoses and isinstance(diagnoses, list):
                mapped_diagnoses = []
                diag_mapping = diagnoses_mapping()
                for diagnosis in diagnoses:
                    if diagnosis and diagnosis in diag_mapping:
                        mapped_diagnoses.append(diag_mapping[diagnosis])
                if mapped_diagnoses:
                    patient_info["diagnoses"] = ", ".join(mapped_diagnoses)
            
            # Control Level - from top-level controlLevel
            control_level = doc.get("controlLevel")
            if control_level:
                patient_info["control_level"] = str(control_level)
        
            # Get nutrition-related information for daily summary (with AI summarization)
            # 获取用于每日总结的营养相关信息（使用AI总结）
            # Try AI summarization first, fall back to basic extraction if fails
            # 先尝试AI总结，如果失败则回退到基本提取
            try:
                cache_db_instance = CacheDB(db_path=Path("./cache.db")) if CacheDB else None
                openai_key = os.getenv("OPENAI_API_KEY")
                if openai_key and OpenAI:
                    nutrition_info = get_nutrition_info_with_ai(
                        client, patient_id, database_name,
                        openai_api_key=openai_key,
                        cache_db=cache_db_instance,
                        debug=False
                    )
                else:
                    nutrition_info = get_nutrition_info(client, patient_id, database_name)
            except Exception as e:
                print(f"[WARN] AI nutrition info summarization failed, using basic extraction: {e}")
                nutrition_info = get_nutrition_info(client, patient_id, database_name)
            
            patient_info.update(nutrition_info)
            
            # Print debug info
            print(f"[DEBUG] Extracted patient info fields: {list(patient_info.keys())}")
            for key, value in patient_info.items():
                if key != "patient_id":  # Skip patient_id in debug output
                    print(f"[DEBUG]   {key}: {value} (type: {type(value).__name__})")
            
            return patient_info
        else:
            print(f"[INFO] Could not find patient in uc_patients collection for {patient_id}")
    except Exception as e:
        print(f"[ERROR] Error querying uc_patients collection: {e}")
    
    print(f"[INFO] Could not find detailed patient info for {patient_id}, using basic info (patient_id only)")
    return patient_info  # Return at least patient_id


# Removed extract_image_links_from_row - we'll get images directly from API


def get_food_log_image_urls(
    food_logs_df: pd.DataFrame,
    session_token: Optional[str],
    debug: bool = False
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Get image URLs for food logs from API.
    从API获取食物日志的图片URL。
    
    Logic: For each food log ID, call GET {UC_BACKEND_API_BASE_URL}/food-log/{foodLogId}
    Images are in data.images[].link - directly use these URLs, no download needed.
    
    Args:
        food_logs_df: DataFrame with food logs (must have _id column)
        session_token: Session token for API (required)
        debug: Enable debug mode
        
    Returns:
        Tuple of (DataFrame with ImageURLs column added, dict of raw API responses for debug)
    """
    api_responses = {}  # Store raw API responses for debug
    
    if not session_token:
        print("[WARN] No session token provided, cannot get image URLs from API")
        return food_logs_df, api_responses
    
    if "ImageURLs" not in food_logs_df.columns:
        food_logs_df["ImageURLs"] = None
    if "ImgName" not in food_logs_df.columns:
        food_logs_df["ImgName"] = ""
    
    # Determine ID column (use _id from MongoDB)
    id_col = None
    for col in ["_id", "FoodLogId", "foodLogId"]:
        if col in food_logs_df.columns:
            id_col = col
            break
    
    if not id_col:
        print("[WARN] No food log ID column found, skipping image URL retrieval")
        return food_logs_df, api_responses
    
    with httpx.Client(timeout=15) as client:
        for idx, row in food_logs_df.iterrows():
            fid = str(row[id_col]).strip()
            if not fid or fid.lower() == "nan":
                continue
            
            if debug:
                print(f"[DEBUG] Processing FoodLog ID: {fid}")
            
            # Skip if already has URLs
            if pd.notna(row.get("ImageURLs")):
                if debug:
                    print(f"[DEBUG] FoodLog {fid}: Already has image URLs, skipping")
                continue
            
            # Call API: GET {UC_BACKEND_API_BASE_URL}/food-log/{foodLogId}
            url = f"{API_BASE}/{fid}"
            if debug:
                print(f"[DEBUG] FoodLog {fid}: Calling API: {url}")
            
            try:
                resp = client.get(url, headers=make_headers(session_token))
                resp.raise_for_status()
                payload = resp.json()
                
                # Store raw API response for debug
                if debug:
                    api_responses[fid] = payload
                
                if debug:
                    print(f"[DEBUG] FoodLog {fid}: API response received")
                    print(f"[DEBUG] FoodLog {fid}: Full payload structure:")
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                    
                    data = payload.get("data", {})
                    if isinstance(data, dict):
                        print(f"[DEBUG] FoodLog {fid}: data structure:")
                        print(json.dumps(data, indent=2, ensure_ascii=False))
                        
                        images = data.get("images", [])
                        print(f"[DEBUG] FoodLog {fid}: Found {len(images) if isinstance(images, list) else 0} image(s) in response")
                        if isinstance(images, list) and len(images) > 0:
                            for i, img_item in enumerate(images):
                                if isinstance(img_item, dict):
                                    print(f"[DEBUG] FoodLog {fid}: Image {i+1} keys: {list(img_item.keys())}")
                                    print(f"[DEBUG] FoodLog {fid}: Image {i+1} full data:")
                                    print(json.dumps(img_item, indent=2, ensure_ascii=False))
                                    if 'link' in img_item:
                                        print(f"[DEBUG] FoodLog {fid}: Image {i+1} 'link': {img_item.get('link')[:100]}..." if len(str(img_item.get('link'))) > 100 else f"[DEBUG] FoodLog {fid}: Image {i+1} 'link': {img_item.get('link')}")
                                    if 'fileKey' in img_item:
                                        print(f"[DEBUG] FoodLog {fid}: Image {i+1} 'fileKey': {img_item.get('fileKey')} (ignored, using 'link' instead)")
                
                # Extract image links from data.images[].link (NOT fileKey)
                links = extract_links(payload, debug=debug)
                
                if links:
                    # Store URLs directly - no download needed
                    food_logs_df.at[idx, "ImageURLs"] = links
                    food_logs_df.at[idx, "ImgName"] = ";".join(links)  # For compatibility
                    print(f"[OK] FoodLog {fid}: Got {len(links)} image URL(s) from 'link' field")
                    if debug:
                        for i, link in enumerate(links):
                            print(f"[DEBUG] FoodLog {fid}: Image URL {i+1}: {link[:100]}..." if len(link) > 100 else f"[DEBUG] FoodLog {fid}: Image URL {i+1}: {link}")
                else:
                    print(f"[WARN] FoodLog {fid}: No 'link' found in data.images[].link")
                
                # Extract description and comments from API response
                # 从API响应中提取description和comments
                data = payload.get("data", {})
                if isinstance(data, dict):
                    # Extract description (note field in API)
                    if "note" in data and data["note"]:
                        food_logs_df.at[idx, "Description"] = str(data["note"])
                    elif "description" in data and data["description"]:
                        food_logs_df.at[idx, "Description"] = str(data["description"])
                    
                    # Extract comments
                    if "comments" in data and data["comments"]:
                        # Store comments directly - can be list or dict
                        food_logs_df.at[idx, "comments"] = data["comments"]
                        if debug:
                            print(f"[DEBUG] FoodLog {fid}: Found comments: {data['comments']}")
                    # Always show raw data when no links found (even without debug mode)
                    print(f"[DEBUG] FoodLog {fid}: Raw API response data:")
                    data = payload.get("data", {})
                    if isinstance(data, dict):
                        print(json.dumps(data, indent=2, ensure_ascii=False))
                        images = data.get("images", [])
                        if images:
                            print(f"[DEBUG] FoodLog {fid}: images array has {len(images)} items")
                            if isinstance(images, list) and len(images) > 0:
                                first_item = images[0]
                                print(f"[DEBUG] FoodLog {fid}: First image item type: {type(first_item)}")
                                if isinstance(first_item, dict):
                                    print(f"[DEBUG] FoodLog {fid}: First image item keys: {list(first_item.keys())}")
                                    print(f"[DEBUG] FoodLog {fid}: First image item full data:")
                                    print(json.dumps(first_item, indent=2, ensure_ascii=False))
                                    print(f"[DEBUG] FoodLog {fid}: Has 'link' key: {'link' in first_item}, Has 'fileKey' key: {'fileKey' in first_item}")
                                    if 'link' in first_item:
                                        link_val = first_item.get('link')
                                        print(f"[DEBUG] FoodLog {fid}: 'link' value type: {type(link_val)}, value: {link_val}")
                                    if 'fileKey' in first_item:
                                        print(f"[DEBUG] FoodLog {fid}: 'fileKey' value: {first_item.get('fileKey')} (ignored)")
            except httpx.HTTPStatusError as e:
                print(f"[ERROR] FoodLog {fid}: HTTP {e.response.status_code}")
                if debug:
                    print(f"[DEBUG] FoodLog {fid}: Response body: {e.response.text[:500]}")
            except Exception as e:
                print(f"[WARN] FoodLog {fid}: Failed to get image URLs: {e}")
                if debug:
                    import traceback
                    print(f"[DEBUG] FoodLog {fid}: Exception traceback:")
                    traceback.print_exc()
    
    return food_logs_df, api_responses


def download_image_with_cache(
    image_url: str,
    images_dir: Path,
    food_log_id: Optional[str] = None,
    image_index: Optional[int] = None,
    patient_id: Optional[str] = None,
    date: Optional[str] = None,
    cache_db: Optional[CacheDB] = None,
    debug: bool = False
) -> Optional[Path]:
    """
    Download image with caching support.
    下载图片并支持缓存。
    
    Args:
        image_url: Image URL / 图片URL
        images_dir: Directory to save images / 保存图片的目录
        food_log_id: Food log ID for naming (preferred) / 用于命名的 Food log ID（优先使用）
        image_index: Image index (0-based) for naming / 用于命名的图片索引（从0开始）
        patient_id: Patient ID for naming / 用于命名的病人ID
        date: Date string (YYYY-MM-DD) for naming / 用于命名的日期字符串（YYYY-MM-DD）
        cache_db: Cache database instance (optional) / 缓存数据库实例（可选）
        debug: Enable debug mode / 启用调试模式
        
    Returns:
        Local file path or None if download failed / 本地文件路径或下载失败时返回None
    """
    # Check cache first
    # 首先检查缓存
    if cache_db and food_log_id is not None and image_index is not None:
        cached = cache_db.get_image_cache(food_log_id=food_log_id, image_index=image_index)
        if cached:
            local_path = Path(cached["local_path"])
            if local_path.exists():
                if debug:
                    print(f"[DEBUG] Using cached image: {local_path}")
                return local_path
    
    # Also check by image_url as fallback
    # 也通过 image_url 检查作为备用
    if cache_db:
        cached = cache_db.get_image_cache(image_url=image_url)
        if cached:
            local_path = Path(cached["local_path"])
            if local_path.exists():
                if debug:
                    print(f"[DEBUG] Using cached image (by URL): {local_path}")
                return local_path
    
    # Download image
    # 下载图片
    try:
        images_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename: patient_id_food_log_id_date.jpg
        # 生成文件名：patient_id_food_log_id_date.jpg
        ext = guess_ext_from_url(image_url)
        if patient_id and food_log_id and date:
            # New format: patient_id_food_log_id_date.jpg
            # 新格式：patient_id_food_log_id_date.jpg
            if image_index > 0:
                filename = f"{patient_id}_{food_log_id}_{date}_{image_index}{ext}"
            else:
                filename = f"{patient_id}_{food_log_id}_{date}{ext}"
        elif food_log_id is not None and image_index is not None:
            # Fallback to old format: food_log_id_image_index
            # 回退到旧格式：food_log_id_image_index
            if image_index > 0:
                filename = f"{food_log_id}_{image_index}{ext}"
            else:
                filename = f"{food_log_id}{ext}"
        else:
            # Fallback to URL hash
            # 回退到 URL 哈希
            import hashlib
            url_hash = hashlib.md5(image_url.encode('utf-8')).hexdigest()
            filename = f"{url_hash}{ext}"
        
        local_path = images_dir / filename
        
        if debug:
            print(f"[DEBUG] Downloading image: {image_url[:100]}... -> {local_path}")
        
        with httpx.Client(timeout=30) as client:
            response = client.get(image_url)
            response.raise_for_status()
            
            # Save to file
            # 保存到文件
            local_path.write_bytes(response.content)
            
            # Compute file hash
            # 计算文件哈希
            import hashlib
            file_hash = hashlib.md5(response.content).hexdigest()
            file_size = len(response.content)
            
            # Save to cache
            # 保存到缓存
            if cache_db:
                cache_db.save_image_cache(
                    str(local_path),
                    food_log_id=food_log_id,
                    image_index=image_index,
                    image_url=image_url,
                    file_hash=file_hash,
                    file_size=file_size
                )
                if debug:
                    print(f"[DEBUG] Saved image to cache: {local_path}")
            
            return local_path
            
    except Exception as e:
        if debug:
            print(f"[DEBUG] Failed to download image {image_url[:100]}...: {e}")
            import traceback
            traceback.print_exc()
        return None


def parse_meal_type(row: pd.Series, language: str = 'zh') -> str:
    """
    Parse meal type from food log row.
    从食物日志行解析餐次类型。
    
    Args:
        language: 'zh' for Chinese, 'en' for English
    
    Returns: "早餐"/"Breakfast", "午餐"/"Lunch", "晚餐"/"Dinner", or "其他"/"Other"
    """
    meal_translations = {
        'zh': {
            'breakfast': '早餐',
            'lunch': '午餐',
            'dinner': '晚餐',
            'other': '其他'
        },
        'en': {
            'breakfast': 'Breakfast',
            'lunch': 'Lunch',
            'dinner': 'Dinner',
            'other': 'Other'
        }
    }
    t = meal_translations.get(language, meal_translations['zh'])
    # Try different field names for meal title
    meal_title = ""
    for field in ["MealTitle", "mealTitle", "meal_type", "mealType", "meal_title"]:
        if field in row and pd.notna(row[field]):
            meal_title = str(row[field]).strip()
            if meal_title:
                break
    
    # If no meal title, try to infer from createdAt time
    if not meal_title:
        created_at = None
        for field in ["createdAt", "created_at", "uploadedAt", "uploaded_at"]:
            if field in row and pd.notna(row[field]):
                try:
                    created_at = pd.to_datetime(row[field])
                    break
                except:
                    pass
        
        if created_at:
            hour = created_at.hour
            if 5 <= hour < 10:
                return t['breakfast']
            elif 10 <= hour < 14:
                return t['lunch']
            elif 14 <= hour < 20:
                return t['dinner']
            else:
                return t['other']
        
        return t['other']
    
    meal_lower = meal_title.lower()
    
    # English keywords
    if any(keyword in meal_lower for keyword in ["breakfast", "morning"]):
        return t['breakfast']
    if any(keyword in meal_lower for keyword in ["lunch", "noon", "midday"]):
        return t['lunch']
    if any(keyword in meal_lower for keyword in ["dinner", "evening", "night", "supper"]):
        return t['dinner']
    if any(keyword in meal_lower for keyword in ["snack", "snacks"]):
        return t['other']
    
    # Chinese keywords
    if "早餐" in meal_title:
        return t['breakfast']
    if "午餐" in meal_title:
        return t['lunch']
    if "晚餐" in meal_title or "晚饭" in meal_title:
        return t['dinner']
    
    # Chinese
    if "早餐" in meal_title or "早饭" in meal_title or "早" in meal_title:
        return "早餐"
    if "午餐" in meal_title or "午饭" in meal_title or "午" in meal_title:
        return "午餐"
    if "晚餐" in meal_title or "晚饭" in meal_title or "晚" in meal_title or "夜" in meal_title:
        return "晚餐"
    
    return t['other']


def analyze_food_image_with_openai(
    image_url: str,
    openai_api_key: Optional[str] = None,
    prompt_file: Optional[Path] = None,
    patient_notes: Optional[str] = None,
    food_log_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    date: Optional[str] = None,
    cache_db: Optional[CacheDB] = None,
    regenerate: bool = False,
    debug: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Analyze food image using OpenAI Vision API to generate meal summary.
    使用 OpenAI Vision API 分析食物图片生成 meal summary。
    
    Args:
        image_url: URL of the food image / 食物图片的URL
        openai_api_key: OpenAI API key (if None, will try to get from env) / OpenAI API key
        prompt_file: Path to prompt file (default: food_image_meal_summary_prompt.txt) / prompt文件路径
        patient_notes: Patient notes content to include in analysis (optional) / 包含在分析中的病人备注内容（可选）
        food_log_id: Food log ID for caching (preferred) / 用于缓存的 Food log ID（优先使用）
        cache_db: Cache database instance (optional) / 缓存数据库实例（可选）
        debug: Enable debug mode / 启用调试模式
        
    Returns:
        Dict with parsed meal summary or None if failed / 解析后的meal summary字典或None
    """
    if OpenAI is None:
        if debug:
            print("[DEBUG] OpenAI package not available, skipping image analysis")
        return None
    
    # Normalize patient_notes: treat empty string as None for consistent caching
    # 规范化 patient_notes：将空字符串视为 None 以保持缓存一致性
    if patient_notes is not None and not patient_notes.strip():
        patient_notes = None
    
    # Check cache first (unless regenerate is True)
    # 首先检查缓存（除非 regenerate 为 True）
    if cache_db and not regenerate:
        if debug:
            if food_log_id:
                print(f"[DEBUG] Checking cache for food_log_id: {food_log_id}")
            else:
                print(f"[DEBUG] Checking cache for image: {image_url[:100]}...")
            if patient_notes:
                print(f"[DEBUG] Patient notes: {patient_notes[:100]}...")
        cached_summary = cache_db.get_ai_summary_cache(food_log_id, image_url, patient_notes)
        if cached_summary:
            if debug:
                print(f"[DEBUG] ✓ Cache HIT! Using cached AI summary")
            else:
                date_str = f", date: {date}" if date else ""
                patient_str = f"patient_id: {patient_id}" if patient_id else "patient_id: N/A"
                food_log_str = f", food_log_id: {food_log_id}" if food_log_id else ", food_log_id: N/A"
                print(f"[INFO] ✓ Using cached AI summary for {patient_str}{food_log_str}{date_str}")
            return cached_summary
        else:
            if debug:
                print(f"[DEBUG] ✗ Cache MISS")
            else:
                date_str = f", date: {date}" if date else ""
                patient_str = f"patient_id: {patient_id}" if patient_id else "patient_id: N/A"
                food_log_str = f", food_log_id: {food_log_id}" if food_log_id else ", food_log_id: N/A"
                print(f"[INFO] ✗ Cache miss, generating new AI summary for {patient_str}{food_log_str}{date_str}")
    
    # Get API key from parameter or environment
    # 从参数或环境变量获取API key
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        if debug:
            print("[DEBUG] OPENAI_API_KEY not found, skipping image analysis")
        return None
    
    # Load prompt from file
    # 从文件加载prompt
    if prompt_file is None:
        prompt_file = Path(__file__).parent / "food_image_meal_summary_prompt.txt"
    
    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except Exception as e:
        if debug:
            print(f"[DEBUG] Failed to read prompt file: {e}")
        return None
    
    # Append patient notes to prompt if available
    # 如果有病人备注，将其添加到 prompt 中
    if patient_notes and patient_notes.strip():
        patient_notes_section = f"\n\nPATIENT NOTES:\n{patient_notes.strip()}\n\nPlease consider the patient notes above when analyzing the meal image. The notes may contain relevant dietary restrictions, preferences, health conditions, or other context that should inform your analysis."
        prompt_text = prompt_text + patient_notes_section
        if debug:
            print(f"[DEBUG] Added patient notes to prompt ({len(patient_notes)} characters)")
    
    try:
        client = OpenAI(api_key=api_key)
        
        if debug:
            print(f"[DEBUG] Analyzing image with OpenAI: {image_url[:100]}...")
        
        # Call OpenAI Vision API
        # 调用 OpenAI Vision API
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        }
                    ]
                }
            ],
            max_tokens=1000,
            temperature=0.3
        )
        
        # Parse response
        # 解析响应
        content = response.choices[0].message.content
        if debug:
            print(f"[DEBUG] OpenAI response received: {len(content)} characters")
        
        # Parse the structured response
        # 解析结构化响应
        summary = parse_meal_summary_response(content)
        
        if debug:
            print(f"[DEBUG] Parsed meal summary: {summary.get('ai_title', 'N/A')}")
        
        # Save to cache
        # 保存到缓存
        if cache_db and summary:
            cache_db.save_ai_summary_cache(summary, food_log_id, image_url, patient_notes)
            if debug:
                print(f"[DEBUG] Saved AI summary to cache for food_log_id: {food_log_id or 'N/A'}")
        
        return summary
        
    except Exception as e:
        if debug:
            print(f"[DEBUG] OpenAI API call failed: {e}")
            import traceback
            traceback.print_exc()
        return None


def parse_meal_summary_response(response_text: str) -> Dict[str, Any]:
    """
    Parse OpenAI response text into structured meal summary.
    解析 OpenAI 响应文本为结构化的 meal summary。
    
    Args:
        response_text: Raw response text from OpenAI / OpenAI 原始响应文本
        
    Returns:
        Dict with parsed fields / 解析后的字段字典
    """
    summary = {
        "ai_title": "",
        "detected_foods": [],
        "composition": {"carb": None, "protein": None, "veg": None, "fat": None},
        "observations": [],
        "dietary_consideration": ""
    }
    
    try:
        lines = response_text.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse AI title
            # 解析 AI title
            if line.startswith("AI title:"):
                summary["ai_title"] = line.replace("AI title:", "").strip()
                continue
            
            # Parse detected foods
            # 解析检测到的食物
            if line.startswith("Detected foods:"):
                current_section = "foods"
                continue
            elif current_section == "foods" and line.startswith("-"):
                food = line.replace("-", "").strip()
                if food:
                    summary["detected_foods"].append(food)
                continue
            
            # Parse composition
            # 解析成分
            if line.startswith("Composition estimate"):
                current_section = "composition"
                continue
            elif current_section == "composition":
                if "carb:" in line.lower():
                    try:
                        val = float(line.split(":")[-1].strip())
                        summary["composition"]["carb"] = val
                    except:
                        pass
                elif "protein:" in line.lower():
                    try:
                        val = float(line.split(":")[-1].strip())
                        summary["composition"]["protein"] = val
                    except:
                        pass
                elif "veg:" in line.lower():
                    try:
                        val = float(line.split(":")[-1].strip())
                        summary["composition"]["veg"] = val
                    except:
                        pass
                elif "fat:" in line.lower():
                    try:
                        val = float(line.split(":")[-1].strip())
                        summary["composition"]["fat"] = val
                    except:
                        pass
                continue
            
            # Parse observations
            # 解析观察
            if line.startswith("Observations:"):
                current_section = "observations"
                continue
            elif current_section == "observations" and line.startswith("-"):
                obs_line = line.replace("-", "").strip()
                # Parse observation with confidence
                # 解析带置信度的观察
                if "(" in obs_line and "confidence:" in obs_line.lower():
                    parts = obs_line.split("(")
                    keyword = parts[0].strip()
                    try:
                        conf_part = parts[1].split(":")[-1].replace(")", "").strip()
                        confidence = float(conf_part)
                    except:
                        confidence = 1.0
                    summary["observations"].append({"keyword": keyword, "confidence": confidence})
                else:
                    summary["observations"].append({"keyword": obs_line, "confidence": 1.0})
                continue
            
            # Parse dietary consideration
            # 解析饮食考虑
            if line.startswith("Dietary consideration:"):
                summary["dietary_consideration"] = line.replace("Dietary consideration:", "").strip()
                current_section = None
                continue
    
    except Exception as e:
        # If parsing fails, store raw text
        # 如果解析失败，存储原始文本
        summary["raw_response"] = response_text
        summary["parse_error"] = str(e)
    
    return summary


def format_rd_comments(comments_data, language: str = 'zh') -> List[Dict[str, Any]]:
    """
    Format RD/dietitian comments from food log data.
    格式化食物日志中的营养师评论。
    
    Args:
        comments_data: Comments data (can be JSON string, dict, list, or None)
        language: Language for labels ('zh' or 'en')
        
    Returns:
        List of formatted comment dicts with 'text', 'commentedAt', 'commentedBy' fields
    """
    if comments_data is None:
        return []
    
    formatted_comments = []
    
    # Parse JSON if it's a string
    obj = comments_data
    if isinstance(comments_data, str):
        comments_data_strip = comments_data.strip()
        if not comments_data_strip:
            return []
        # Try to parse as JSON
        try:
            obj = json.loads(comments_data_strip)
        except (json.JSONDecodeError, ValueError):
            # If not JSON, treat as plain text
            if comments_data_strip:
                formatted_comments.append({
                    "text": comments_data_strip,
                    "commentedAt": None,
                    "commentedBy": None
                })
            return formatted_comments
    
    # Handle list of comments
    if isinstance(obj, list):
        for comment in obj:
            if isinstance(comment, dict):
                text = comment.get("text") or comment.get("originalText") or comment.get("Text")
                commented_at = comment.get("commentedAt") or comment.get("commented_at")
                commented_by = comment.get("commentedBy") or comment.get("commentedByUser")
                if text:
                    formatted_comments.append({
                        "text": str(text),
                        "commentedAt": commented_at,
                        "commentedBy": commented_by
                    })
    
    # Handle single comment dict
    elif isinstance(obj, dict):
        text = obj.get("text") or obj.get("originalText") or obj.get("Text")
        commented_at = obj.get("commentedAt") or obj.get("commented_at")
        commented_by = obj.get("commentedBy") or obj.get("commentedByUser")
        if text:
            formatted_comments.append({
                "text": str(text),
                "commentedAt": commented_at,
                "commentedBy": commented_by
            })
    
    return formatted_comments


def extract_ingredients_summary(ingredients_data: Any) -> Dict[str, Any]:
    """
    Extract ingredients summary from ingredients data.
    从食材数据中提取食材总结。
    
    Returns:
        Dict with main_ingredients (list of {name, quantity}) and notes
    """
    main_ingredients = []
    notes = []
    
    if not ingredients_data:
        return {"main_ingredients": [], "notes": []}
    
    # Parse JSON if string
    if isinstance(ingredients_data, str):
        ingredients_data = ingredients_data.strip()
        if not ingredients_data:
            return {"main_ingredients": [], "notes": []}
        if ingredients_data.startswith("{") or ingredients_data.startswith("["):
            try:
                ingredients_data = json.loads(ingredients_data)
            except Exception:
                return {"main_ingredients": [], "notes": []}
        else:
            return {"main_ingredients": [], "notes": []}
    
    # Handle list of ingredients
    ingredients_list = []
    if isinstance(ingredients_data, list):
        ingredients_list = ingredients_data
    elif isinstance(ingredients_data, dict):
        ingredients_list = [ingredients_data]
    
    for ingredient in ingredients_list:
        if isinstance(ingredient, dict):
            name = ingredient.get("name") or ingredient.get("Name") or ""
            portion = ingredient.get("estimatedPortion") or ingredient.get("Portion") or ingredient.get("portion") or ""
            
            if name:
                main_ingredients.append({
                    "name": name,
                    "quantity": portion
                })
    
    return {
        "main_ingredients": main_ingredients,
        "notes": notes
    }


def group_food_logs_by_meal(food_logs_df: pd.DataFrame, language: str = 'zh') -> Dict[str, List[pd.Series]]:
    """
    Group food logs by meal type.
    按餐次类型分组食物日志。
    
    Args:
        language: 'zh' for Chinese, 'en' for English
    
    Returns:
        Dict with keys: "早餐"/"Breakfast", "午餐"/"Lunch", "晚餐"/"Dinner", "其他"/"Other"
    """
    grouped = defaultdict(list)
    
    for _, row in food_logs_df.iterrows():
        meal_type = parse_meal_type(row, language=language)
        grouped[meal_type].append(row)
    
    return dict(grouped)


def _format_behavior_goals_list(items: List[Dict[str, Any]]) -> str:
    """Format uc_behavior_goals behaviorGoals list (behaviorGoalType, goalStatus, behaviorGoalValue)."""
    parts = []
    for i, item in enumerate(items[:10]):
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        t = item.get("behaviorGoalType") or item.get("behaviorGoal") or ""
        s = item.get("goalStatus") or item.get("status") or ""
        v = item.get("behaviorGoalValue") or item.get("value") or ""
        line = " / ".join(x for x in [str(t), str(s), str(v)] if x)
        if line:
            parts.append(line)
    return "; ".join(parts) if parts else ""


def _format_clinical_goals_list(items: List[Dict[str, Any]]) -> str:
    """Format uc_clinical_goals clinicalGoals list (Goal: condition, clinicalGoalName, isManualInput)."""
    parts = []
    for item in items[:10]:
        if isinstance(item, dict):
            c = item.get("condition") or item.get("conditionName") or ""
            n = item.get("clinicalGoalName") or item.get("goalName") or ""
            m = item.get("isManualInput")
            line = " / ".join(x for x in [str(c), str(n), str(m)] if x != "" and x is not None)
            parts.append(line if line else json.dumps(item, ensure_ascii=False, default=str))
        else:
            parts.append(str(item))
    return "; ".join(parts) if parts else ""


def _format_monthly_review_goals_list(items: List[Dict[str, Any]]) -> str:
    """Format uc_monthly_review goals list (condition, clinicalGoalName, isManualInput)."""
    parts = []
    for item in items[:10]:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        c = item.get("condition") or item.get("conditionName") or ""
        n = item.get("clinicalGoalName") or item.get("goalName") or ""
        m = item.get("isManualInput")
        line = " / ".join(x for x in [str(c), str(n), str(m)] if x != "" and x is not None)
        if line:
            parts.append(line)
    return "; ".join(parts) if parts else ""


def _doc_display_text(doc: Dict[str, Any], text_keys: Optional[List[str]] = None) -> str:
    """Extract display text from a doc; fallback to key-value dump. text_keys tried first."""
    if text_keys is None:
        text_keys = ["goal", "goals", "behaviorGoals", "clinicalGoals", "note", "content", "text", "assessment", "summary", "notes"]
    for key in text_keys:
        val = doc.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            if not val:
                continue
            if key == "behaviorGoals" and isinstance(val[0], dict):
                return _format_behavior_goals_list(val)
            if key == "clinicalGoals":
                return _format_clinical_goals_list(val)
            if key == "goals" and isinstance(val[0], dict):
                return _format_monthly_review_goals_list(val)
            return "; ".join(str(v) for v in val[:5])
        if str(val).strip():
            return str(val).strip()
    fields = []
    for key, value in doc.items():
        if key not in ["_id", "memberId", "patient_id", "patientId", "member_id"]:
            try:
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    if key == "behaviorGoals":
                        fields.append(f"{key}: {_format_behavior_goals_list(value)}")
                    elif key == "clinicalGoals":
                        fields.append(f"{key}: {_format_clinical_goals_list(value)}")
                    elif key == "goals":
                        fields.append(f"{key}: {_format_monthly_review_goals_list(value)}")
                    else:
                        fields.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
                elif isinstance(value, (dict, list)):
                    fields.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
                else:
                    fields.append(f"{key}: {value}")
            except (TypeError, ValueError):
                fields.append(f"{key}: {repr(value)}")
    return ", ".join(fields) if fields else ""


def _doc_date_str(doc: Dict[str, Any]) -> str:
    """Format createdAt or updatedAt from doc for display."""
    for key in ["createdAt", "updatedAt", "date"]:
        val = doc.get(key)
        if val is not None:
            try:
                if isinstance(val, str):
                    date_obj = datetime.fromisoformat(val.replace("Z", "+00:00"))
                else:
                    date_obj = val
                return date_obj.strftime("%Y-%m-%d")
            except Exception:
                return str(val)
    return ""


def _format_lifestyle_display(doc: Dict[str, Any], language: str) -> str:
    """Format uc_lifestyle_assessments for display with structured field presentation."""
    lines = []
    
    # Physical Activities
    activities = doc.get("physicalActivities") or doc.get("physical_activities") or []
    if activities and isinstance(activities, list):
        label = "Physical Activities" if language == "en" else "身体活动"
        lines.append(f"{label}:")
        for act in activities[:10]:
            if isinstance(act, dict):
                t = act.get("type", "Unknown").replace("_", " ").title()
                intensity = act.get("intensity", "").replace("_", " ").title()
                freq = act.get("frequency", "").replace("_", " ").lower()
                lines.append(f"  • {t}: {intensity}, {freq}")
            else:
                lines.append(f"  • {act}")
    
    # Smoking/Drinking
    smoke_drink = doc.get("smokeDrinkRecord") or doc.get("smoke_drink_record")
    if smoke_drink and isinstance(smoke_drink, dict):
        label = "Smoking/Drinking" if language == "en" else "吸烟饮酒"
        lines.append(f"{label}:")
        is_smoking = smoke_drink.get("isSmoking") or smoke_drink.get("is_smoking")
        is_drinking = smoke_drink.get("isDrinking") or smoke_drink.get("is_drinking")
        if is_drinking:
            quit_year = smoke_drink.get("quitDrinkingYear") or smoke_drink.get("quit_drinking_year")
            lines.append(f"  • Alcohol: {'Stopped in ' + str(quit_year) if quit_year else 'Currently drinking'}")
        if is_smoking:
            quit_year = smoke_drink.get("quitSmokingYear") or smoke_drink.get("quit_smoking_year")
            lines.append(f"  • Smoking: {'Stopped in ' + str(quit_year) if quit_year else 'Currently smoking'}")
    
    # Fast Food
    fast_food = doc.get("fastFoodFreq") or doc.get("fast_food_freq") or []
    if fast_food and isinstance(fast_food, list):
        label = "Fast Food Frequency" if language == "en" else "快餐频率"
        lines.append(f"{label}:")
        for item in fast_food[:5]:
            if isinstance(item, dict):
                t = item.get("type", "Fast food")
                v = item.get("value", "")
                u = item.get("unit", "").lower()
                lines.append(f"  • {t}: {v} times {u}")
    
    # Beverages
    beverages = doc.get("beverageFreq") or doc.get("beverage_freq") or []
    if beverages and isinstance(beverages, list):
        label = "Beverage Frequency" if language == "en" else "饮料频率"
        lines.append(f"{label}:")
        for item in beverages[:5]:
            if isinstance(item, dict):
                t = item.get("type", "Beverage")
                v = item.get("value", "")
                u = item.get("unit", "").lower()
                lines.append(f"  • {t}: {v} times {u}")
    
    # Nutrition Understanding
    nu = doc.get("nutritionUnderstanding") or doc.get("nutrition_understanding")
    if nu and str(nu).strip():
        label = "Nutrition Understanding" if language == "en" else "营养理解"
        lines.append(f"{label}: {str(nu).strip()}")
    
    # Meal & Sleep Routines
    routines = doc.get("mealAndSleepRoutines") or doc.get("meal_and_sleep_routines") or []
    if routines and isinstance(routines, list):
        label = "Meal & Sleep Routines" if language == "en" else "饮食睡眠规律"
        lines.append(f"{label}:")
        for r in routines[:10]:
            if isinstance(r, dict):
                t = r.get("type", "").replace("_", " ").title()
                if t in ["Wakeup", "Sleep"]:
                    start = r.get("startTime") or r.get("start_time")
                    lines.append(f"  • {t}: {start if start else 'N/A'}")
                else:
                    food = r.get("foodTypeAmount") or r.get("food_type_amount") or "N/A"
                    lines.append(f"  • {t}: {food}")
    
    # Previous Diets
    prev = doc.get("previousDiets") or doc.get("previous_diets") or []
    if prev and isinstance(prev, list):
        label = "Previous Diets" if language == "en" else "既往饮食"
        lines.append(f"{label}: {', '.join(str(d).replace('_', ' ').title() for d in prev[:10])}")
    
    # Is Eating Fast Food
    is_ff = doc.get("isEatingFastFood") or doc.get("is_eating_fast_food")
    if is_ff is not None:
        label = "Eating Fast Food" if language == "en" else "是否吃快餐"
        lines.append(f"{label}: {'Yes' if is_ff else 'No'}")
    
    # Stress Management
    stress = doc.get("stressManagement") or doc.get("stress_management")
    if stress and isinstance(stress, dict):
        label = "Stress Management" if language == "en" else "压力管理"
        level = stress.get("stressLevel") or stress.get("stress_level") or ""
        strategy = stress.get("strategy") or ""
        depression = stress.get("recentDepression") or stress.get("recent_depression") or ""
        lines.append(f"{label}: Level={level}, Strategy={strategy}, Recent Depression={depression}")
    
    # Additional Comments (shown separately, but include here for completeness)
    additional = doc.get("additionalComments") or doc.get("additional_comments")
    if additional and str(additional).strip():
        label = "Additional Comments" if language == "en" else "补充说明"
        lines.append(f"{label}: {str(additional).strip()}")
    
    return "\n".join(lines) if lines else ""


def _format_nutrition_display(doc: Dict[str, Any], language: str) -> str:
    """Format uc_nutritions (Dietary) for display, matching agent-service Dietary.description format."""
    lines = []
    
    # Updated At
    updated_at = doc.get("updatedAt") or doc.get("updated_at")
    if updated_at:
        try:
            if isinstance(updated_at, str):
                date_str = updated_at[:10]
            else:
                date_str = updated_at.strftime('%Y-%m-%d')
            label = "Last updated" if language == "en" else "最后更新"
            lines.append(f"{label}: {date_str}")
        except Exception:
            pass
    
    # Fast Food Frequency
    ff = doc.get("fastFoodFreq") or doc.get("fast_food_freq")
    if ff and isinstance(ff, dict):
        v = ff.get("value", "")
        u = ff.get("unit", "").lower() if ff.get("unit") else ""
        if v and u:
            label = "Fast Food Frequency" if language == "en" else "快餐频率"
            lines.append(f"{label}: {v} times {u}")
    
    # Sweet Beverage Frequency
    sb = doc.get("sweetBeverageFreq") or doc.get("sweet_beverage_freq")
    if sb and isinstance(sb, dict):
        v = sb.get("value", "")
        u = sb.get("unit", "").lower() if sb.get("unit") else ""
        if v and u:
            label = "Sweet Beverage Frequency" if language == "en" else "甜饮料频率"
            lines.append(f"{label}: {v} times {u}")
    
    # Nutrition Understanding
    nu = doc.get("nutritionUnderstanding") or doc.get("nutrition_understanding")
    if nu and str(nu).strip():
        label = "Nutrition Understanding" if language == "en" else "营养理解"
        lines.append(f"{label}: {str(nu).strip()}")
    
    # Intake (Meal Intake Details)
    intake = doc.get("intake") or []
    if intake and isinstance(intake, list):
        lines.append("")  # Empty line before intake section
        label = "Meal Intake Details" if language == "en" else "饮食摄入详情"
        lines.append(f"{label}:")
        for item in intake[:10]:
            if isinstance(item, dict):
                meal = item.get("meal", "Unknown Meal")
                if meal and isinstance(meal, str):
                    meal = meal.replace("_", " ").title()
                time_range = item.get("timeRange") or item.get("time_range") or "Unknown Time"
                food = item.get("foodTypeAmount") or item.get("food_type_amount") or "Unknown Amount"
                freq = item.get("mealFreq") or item.get("meal_freq") or "Unknown Frequency"
                lines.append(f"- {meal}: Time Range: {time_range}, Food Amount: {food}, Frequency: {freq}")
    
    # Previous Diets
    prev = doc.get("previousDiets") or doc.get("previous_diets") or []
    if prev and isinstance(prev, list) and prev:
        label = "Previous Diets" if language == "en" else "既往饮食"
        diets_str = ", ".join(str(d).replace("_", " ").title() for d in prev[:10] if d)
        if diets_str:
            lines.append(f"{label}: {diets_str}")
    
    return "\n".join(lines) if lines else ""


def build_patient_info_html(
    patient_info: Dict[str, Any],
    patient_id: str,
    language: str = 'zh',
    care_notes: Optional[List[Dict[str, Any]]] = None,
    behavior_goals: Optional[List[Dict[str, Any]]] = None,
    monthly_reviews: Optional[List[Dict[str, Any]]] = None,
    clinical_goals: Optional[List[Dict[str, Any]]] = None,
    lifestyle_assessments: Optional[List[Dict[str, Any]]] = None,
    nutritions: Optional[List[Dict[str, Any]]] = None,
    chat_messages: Optional[List[Dict[str, Any]]] = None,
    pre_visit_history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """
    Build only the Patient Information HTML block (for display on main page or in summary).
    Returns HTML string for patient details, care notes, behavior goals, monthly reviews, clinical goals, lifestyle assessments, nutritions, chat history, pre-visit summary history.
    """
    labels = {
        'zh': {
            'patient_info': '患者信息',
            'years_old': '岁',
            'male': '男性',
            'female': '女性',
            'weight': '体重',
            'height': '身高',
            'medical_history': '疾病史',
            'ethnicity': '民族',
            'region': '地域',
            'exercise_intensity': '运动强度',
            'medications': '当前用药',
        },
        'en': {
            'patient_info': 'Patient Information',
            'years_old': ' years old',
            'male': 'Male',
            'female': 'Female',
            'weight': 'Weight',
            'height': 'Height',
            'medical_history': 'Medical History',
            'ethnicity': 'Ethnicity',
            'region': 'Region',
            'exercise_intensity': 'Exercise Intensity',
            'medications': 'Current Medications',
        }
    }
    t = labels.get(language, labels['zh'])

    patient_info_html = f"""
    <div class="patient-info">
        <h2>{t['patient_info']}</h2>
        <ul class="patient-details">
    """

    age_val = patient_info.get("age")
    if age_val is not None and age_val != "":
        gender_display = patient_info.get("gender_display") or patient_info.get("gender") or ""
        gender_identity = patient_info.get("gender_identity") or ""
        gender_text = ""
        if gender_display:
            gender_text = f", {gender_display}"
        elif gender_display := patient_info.get("gender"):
            gender_lower = str(gender_display).lower().strip()
            if gender_lower in ["male", "m", "男"]:
                gender_text = f", {t['male']}"
            elif gender_lower in ["female", "f", "女"]:
                gender_text = f", {t['female']}"
        gender_identity_text = ""
        if gender_identity:
            gender_identity_text = f" ({gender_identity})"
        age_suffix = t['years_old'] if language == 'zh' else ' years old'
        age_label = "年龄" if language == 'zh' else "Age"
        patient_info_html += f'<li><strong>{age_label}:</strong> {age_val}{age_suffix}{gender_text}{gender_identity_text}</li>'

    weight_val = patient_info.get("weight")
    height_val = patient_info.get("height")
    if weight_val is not None and weight_val != "" and height_val is not None and height_val != "":
        patient_info_html += f'<li><strong>{t["weight"]}:</strong> {weight_val}kg, <strong>{t["height"]}:</strong> {height_val}cm</li>'
    elif weight_val is not None and weight_val != "":
        patient_info_html += f'<li><strong>{t["weight"]}:</strong> {weight_val}kg</li>'
    elif height_val is not None and height_val != "":
        patient_info_html += f'<li><strong>{t["height"]}:</strong> {height_val}cm</li>'

    bmi_val = patient_info.get("bmi")
    if bmi_val is not None and bmi_val != "":
        patient_info_html += f'<li><strong>BMI:</strong> {bmi_val}</li>'

    diagnoses = patient_info.get("diagnoses")
    if diagnoses and str(diagnoses).strip():
        diagnoses_label = "诊断" if language == 'zh' else "Diagnoses"
        patient_info_html += f'<li><strong>{diagnoses_label}:</strong> {html.escape(str(diagnoses))}</li>'

    ethnicity = patient_info.get("ethnicity")
    if ethnicity and str(ethnicity).strip():
        patient_info_html += f'<li><strong>{t["ethnicity"]}:</strong> {html.escape(str(ethnicity))}</li>'

    region = patient_info.get("region")
    if region and str(region).strip():
        patient_info_html += f'<li><strong>{t["region"]}:</strong> {html.escape(str(region))}</li>'

    exercise = patient_info.get("exercise_intensity")
    if exercise and str(exercise).strip():
        patient_info_html += f'<li><strong>{t["exercise_intensity"]}:</strong> {html.escape(str(exercise))}</li>'

    medications = patient_info.get("medications")
    if medications and str(medications).strip():
        patient_info_html += f'<li><strong>{t["medications"]}:</strong> {html.escape(str(medications))}</li>'

    has_detailed_info = any([age_val, weight_val, height_val, bmi_val, diagnoses, ethnicity, region, exercise, medications])

    biggest_problem = patient_info.get("biggest_medical_problem")
    nutrition_goals = patient_info.get("nutrition_behavioral_goals")
    nutrition_assessment = patient_info.get("nutrition_assessment")
    if biggest_problem and str(biggest_problem).strip():
        label = "主要医疗问题" if language == 'zh' else "Primary Medical Problem"
        formatted_problem = format_text_with_line_breaks(str(biggest_problem).strip())
        patient_info_html += f'<li><strong>{label}:</strong><br>{formatted_problem}</li>'
    if nutrition_goals and str(nutrition_goals).strip():
        label = "营养行为目标" if language == 'zh' else "Nutrition Behavioral Goals"
        formatted_goals = format_nutrition_goals(str(nutrition_goals).strip(), language)
        patient_info_html += f'<li><strong>{label}:</strong><br>{formatted_goals}</li>'
    if nutrition_assessment and str(nutrition_assessment).strip():
        label = "初始营养评估" if language == 'zh' else "Initial Nutrition Assessment"
        formatted_assessment = format_text_with_line_breaks(str(nutrition_assessment).strip())
        patient_info_html += f'<li><strong>{label}:</strong><br>{formatted_assessment}</li>'

    has_key_items = any([biggest_problem, nutrition_goals, nutrition_assessment])
    if not has_detailed_info and not has_key_items:
        no_info_msg = "暂无患者信息" if language == 'zh' else "No patient information available"
        patient_info_html += f'<li style="color: #999; font-style: italic;">{no_info_msg}</li>'

    patient_info_html += """
        </ul>
    """

    if care_notes and len(care_notes) > 0:
        care_notes_label = "Care Notes" if language == 'en' else "Care Notes"
        view_details_text = "查看详情" if language == 'zh' else "View Details"
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{care_notes_label}</h3>
            <div style="max-height: 300px; overflow-y: auto;">
        """
        for index, note in enumerate(care_notes):
            full_note_text = ''
            if note.get('note'):
                full_note_text = note['note']
            elif note.get('content'):
                full_note_text = note['content']
            elif note.get('text'):
                full_note_text = note['text']
            else:
                fields = []
                for key, value in note.items():
                    if key not in ['_id', 'memberId', 'patient_id']:
                        if isinstance(value, (dict, list)):
                            fields.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
                        else:
                            fields.append(f"{key}: {value}")
                full_note_text = ', '.join(fields) if fields else 'No content'
            summary = full_note_text[:100] + '...' if len(full_note_text) > 100 else full_note_text
            date_str = ''
            if note.get('createdAt'):
                try:
                    if isinstance(note['createdAt'], str):
                        date_obj = datetime.fromisoformat(note['createdAt'].replace('Z', '+00:00'))
                    else:
                        date_obj = note['createdAt']
                    date_str = date_obj.strftime('%Y-%m-%d')
                except Exception:
                    date_str = str(note.get('createdAt', ''))
            note_id = str(note.get('_id', index))
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 8px; background: #f8f9fa; border-left: 3px solid #4a90e2; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 4px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333; margin-bottom: 6px;">{html.escape(summary)}</div>
                <a href="/care-note/{html.escape(str(patient_id))}/{html.escape(note_id)}" target="_blank" style="font-size: 12px; color: #4a90e2; text-decoration: none; font-weight: 600;">{view_details_text} →</a>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    elif care_notes is not None:
        no_care_notes_text = "暂无Care Notes" if language == 'zh' else "No Care Notes"
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">Care Notes</h3>
            <div style="color: #999; font-style: italic;">{no_care_notes_text}</div>
        </div>
        """

    # uc_behavior_goals
    _section_title_zh = "行为目标 (uc_behavior_goals)"
    _section_title_en = "Behavior Goals (uc_behavior_goals)"
    _no_data_zh = "暂无数据"
    _no_data_en = "No data"
    if behavior_goals and len(behavior_goals) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_section_title_zh if language == 'zh' else _section_title_en}</h3>
            <div style="max-height: 200px; overflow-y: auto;">
        """
        for idx, doc in enumerate(behavior_goals):
            text = _doc_display_text(doc, ["behaviorGoals", "goal", "goals", "nutritionGoal", "nutrition_goal", "behavioralGoal", "behavioral_goal", "note", "content", "text"])
            summary = text
            date_str = _doc_date_str(doc)
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 8px; background: #f8f9fa; border-left: 3px solid #28a745; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 4px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333;">{html.escape(summary)}</div>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_section_title_zh if language == 'zh' else _section_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # uc_monthly_review
    _mr_title_zh = "月度回顾 (uc_monthly_review)"
    _mr_title_en = "Monthly Review (uc_monthly_review)"
    if monthly_reviews and len(monthly_reviews) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_mr_title_zh if language == 'zh' else _mr_title_en}</h3>
            <div style="max-height: 200px; overflow-y: auto;">
        """
        view_details_text = "查看详情" if language == 'zh' else "View Details"
        for idx, doc in enumerate(monthly_reviews):
            # Monthly Review: prioritize note (unique review content), then goals; avoid showing only goals (same as clinical goals)
            note_text = (doc.get("note") or doc.get("assessment") or doc.get("nutritionAssessment")
                         or doc.get("initialAssessment") or doc.get("summary") or doc.get("notes") or "")
            if isinstance(note_text, str) and note_text.strip():
                text = note_text.strip()
            else:
                # Fallback to goals if no note
                text = _doc_display_text(doc, ["goals"])
            if not text:
                text = _doc_display_text(doc, ["assessment", "nutritionAssessment", "initialAssessment", "summary", "notes", "content", "text"])
            goals_text = _doc_display_text(doc, ["goals"])
            if goals_text and text != goals_text:
                text = f"{text}\n\nGoals: {goals_text}"
            summary = (text[:100] + '...') if len(text) > 100 else text
            date_str = _doc_date_str(doc)
            note_id = str(doc.get('_id', idx))
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 8px; background: #f8f9fa; border-left: 3px solid #17a2b8; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 4px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333; margin-bottom: 6px;">{html.escape(summary)}</div>
                <a href="/monthly-review/{html.escape(str(patient_id))}/{html.escape(note_id)}" target="_blank" style="font-size: 12px; color: #4a90e2; text-decoration: none; font-weight: 600;">{view_details_text} →</a>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_mr_title_zh if language == 'zh' else _mr_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # uc_clinical_goals
    _cg_title_zh = "临床目标 (uc_clinical_goals)"
    _cg_title_en = "Clinical Goals (uc_clinical_goals)"
    if clinical_goals and len(clinical_goals) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_cg_title_zh if language == 'zh' else _cg_title_en}</h3>
            <div style="max-height: 200px; overflow-y: auto;">
        """
        for idx, doc in enumerate(clinical_goals):
            text = _doc_display_text(doc, ["clinicalGoals", "goal", "goals", "note", "content", "text"])
            summary = text
            date_str = _doc_date_str(doc)
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 8px; background: #f8f9fa; border-left: 3px solid #6f42c1; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 4px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333;">{html.escape(summary)}</div>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_cg_title_zh if language == 'zh' else _cg_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # uc_lifestyle_assessments (emphasize additionalComments)
    _la_title_zh = "生活方式评估 (uc_lifestyle_assessments)"
    _la_title_en = "Lifestyle Assessment (uc_lifestyle_assessments)"
    _ac_label_zh = "补充说明 (Additional Comments)"
    _ac_label_en = "Additional Comments"
    if lifestyle_assessments and len(lifestyle_assessments) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_la_title_zh if language == 'zh' else _la_title_en}</h3>
            <div style="max-height: 300px; overflow-y: auto;">
        """
        for idx, doc in enumerate(lifestyle_assessments):
            date_str = _doc_date_str(doc)
            formatted = _format_lifestyle_display(doc, language)
            if not formatted:
                formatted = _no_data_zh if language == "zh" else _no_data_en
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 12px; background: #f8f9fa; border-left: 3px solid #fd7e14; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 8px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333; white-space: pre-wrap;">{html.escape(formatted)}</div>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_la_title_zh if language == 'zh' else _la_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # uc_nutritions
    _nut_title_zh = "营养记录 (uc_nutritions)"
    _nut_title_en = "Nutrition Records (uc_nutritions)"
    if nutritions and len(nutritions) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_nut_title_zh if language == 'zh' else _nut_title_en}</h3>
            <div style="max-height: 250px; overflow-y: auto;">
        """
        for idx, doc in enumerate(nutritions):
            date_str = _doc_date_str(doc)
            formatted = _format_nutrition_display(doc, language)
            if not formatted:
                formatted = _doc_display_text(doc, ["assessment", "initialAssessment", "baselineAssessment", "summary", "notes", "note", "content", "text"])
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 12px; background: #f8f9fa; border-left: 3px solid #20c997; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #999; margin-bottom: 8px;">{html.escape(date_str)}</div>' if date_str else ''}
                <div style="font-size: 13px; color: #333; white-space: pre-wrap;">{html.escape(formatted)}</div>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_nut_title_zh if language == 'zh' else _nut_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # Chat History Summary
    _chat_title_zh = "聊天记录摘要"
    _chat_title_en = "Chat History Summary"
    if chat_messages and len(chat_messages) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">
                {_chat_title_zh if language == 'zh' else _chat_title_en}
                <a href="/chat-history/{html.escape(patient_id)}" target="_blank" style="margin-left: 10px; font-size: 13px; color: #4a90e2; text-decoration: none;">
                    {"查看完整历史" if language == 'zh' else "View Full History"} →
                </a>
            </h3>
            <div style="max-height: 300px; overflow-y: auto;">
        """
        # Show last 10 messages as preview
        preview_messages = chat_messages[-10:] if len(chat_messages) > 10 else chat_messages
        for msg in preview_messages:
            payload = msg.get('payload', {})
            text = payload.get('text', '')
            user_role = payload.get('userRole', 'unknown')
            display_name = payload.get('displayName', user_role)
            timestamp = msg.get('timestamp', 0)
            
            # Convert timestamp
            try:
                from datetime import datetime
                # Unix 100-nanosecond units: seconds = timestamp // 10^7 (agent-service unix100ns_to_datetime_str)
                seconds = timestamp // 10**7
                dt = datetime.fromtimestamp(seconds)
                time_str = dt.strftime('%Y-%m-%d %H:%M')
            except Exception:
                time_str = ''
            
            # Color based on role
            border_color = '#2196f3' if user_role == 'patient' else '#9c27b0'
            bg_color = '#e3f2fd' if user_role == 'patient' else '#f3e5f5'
            
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 10px; background: {bg_color}; border-left: 3px solid {border_color}; border-radius: 4px;">
                <div style="font-size: 11px; color: #666; margin-bottom: 4px;">
                    <strong>{html.escape(display_name)}</strong> - {html.escape(time_str)}
                </div>
                <div style="font-size: 13px; color: #333; white-space: pre-wrap;">{html.escape(text[:200] + ('...' if len(text) > 200 else ''))}</div>
            </div>
            """
        
        if len(chat_messages) > 10:
            patient_info_html += f"""
            <div style="text-align: center; margin-top: 10px; padding: 10px; background: #f8f9fa; border-radius: 4px;">
                <a href="/chat-history/{html.escape(patient_id)}" target="_blank" style="color: #4a90e2; text-decoration: none; font-weight: 600;">
                    {"查看全部 " + str(len(chat_messages)) + " 条消息" if language == 'zh' else f"View all {len(chat_messages)} messages"} →
                </a>
            </div>
            """
        
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_chat_title_zh if language == 'zh' else _chat_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    # Pre-Visit Summary History (from MySQL pre_visit_summary_history)
    _pv_title_zh = "Pre-Visit Summary 历史"
    _pv_title_en = "Pre-Visit Summary History"
    if pre_visit_history and len(pre_visit_history) > 0:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_pv_title_zh if language == 'zh' else _pv_title_en}</h3>
            <div style="max-height: 300px; overflow-y: auto;">
        """
        for rec in pre_visit_history:
            gen_time = rec.get("generation_time", "")
            summary = rec.get("full_summary", "")
            summary_display = (summary[:300] + "...") if len(summary) > 300 else summary
            patient_info_html += f"""
            <div style="margin: 8px 0; padding: 12px; background: #f0f7ff; border-left: 3px solid #0066cc; border-radius: 4px;">
                {f'<div style="font-size: 11px; color: #666; margin-bottom: 8px;">{html.escape(gen_time)}</div>' if gen_time else ''}
                <div style="font-size: 13px; color: #333; white-space: pre-wrap;">{html.escape(summary_display)}</div>
            </div>
            """
        patient_info_html += """
            </div>
        </div>
        """
    else:
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{_pv_title_zh if language == 'zh' else _pv_title_en}</h3>
            <div style="color: #999; font-style: italic;">{_no_data_zh if language == 'zh' else _no_data_en}</div>
        </div>
        """

    patient_info_html += """
    </div>
    """
    return patient_info_html


def generate_html_summary(
    patient_info: Dict[str, Any],
    food_logs_by_meal: Dict[str, List[pd.Series]],
    images_dir: Path,
    date: str,
    patient_id: str,
    use_data_uri: bool = True,
    image_base_url: Optional[str] = None,
    language: str = 'zh',
    care_notes: Optional[List[Dict[str, Any]]] = None,
    meal_summaries: Optional[Dict[str, Dict[str, Any]]] = None,
    cache_db: Optional[CacheDB] = None,
    include_patient_info: bool = True
) -> str:
    """
    Generate HTML summary similar to the attached image format.
    生成类似附图的HTML总结。
    """
    
    # Initialize meal_summaries if None
    # 如果 meal_summaries 为 None，初始化为空字典
    if meal_summaries is None:
        meal_summaries = {}
    
    # Translations
    labels = {
        'zh': {
            'patient_info': '患者信息',
            'years_old': '岁',
            'male': '男性',
            'female': '女性',
            'weight': '体重',
            'height': '身高',
            'medical_history': '疾病史',
            'ethnicity': '民族',
            'region': '地域',
            'exercise_intensity': '运动强度',
            'medications': '当前用药',
            'food_log_summary': '1日食物日志总结',
            'total_calories': '总热量',
            'kcal': 'kcal',
            'main_ingredients': '主要食材',
            'notes': '病人备注',
            'no_food_logs': '该日期没有食物日志记录',
            'logged_at': '记录时间',
            'lab_results': '实验室检查结果',
            'glucose_fasting': '空腹血糖',
            'hba1c': 'HbA1c',
            'cholesterol_total': '总胆固醇',
            'ldl': 'LDL',
            'hdl': 'HDL',
            'triglycerides': '甘油三酯',
            'blood_pressure': '血压',
            'last_updated': '最后更新',
            'glucose_data': '血糖数据',
            'average': '平均',
            'min': '最低',
            'max': '最高',
            'measurements': '测量次数',
            'mg_dl': 'mg/dL',
            'percent': '%'
        },
        'en': {
            'patient_info': 'Patient Information',
            'years_old': ' years old',
            'male': 'Male',
            'female': 'Female',
            'weight': 'Weight',
            'height': 'Height',
            'medical_history': 'Medical History',
            'ethnicity': 'Ethnicity',
            'region': 'Region',
            'exercise_intensity': 'Exercise Intensity',
            'medications': 'Current Medications',
            'food_log_summary': '1-Day Food Log Summary',
            'total_calories': 'Total Calories',
            'kcal': 'kcal',
            'main_ingredients': 'Main Ingredients',
            'notes': 'Patient Notes',
            'no_food_logs': 'No food log records for this date',
            'logged_at': 'Logged at',
            'lab_results': 'Lab Results',
            'glucose_fasting': 'Fasting Glucose',
            'hba1c': 'HbA1c',
            'cholesterol_total': 'Total Cholesterol',
            'ldl': 'LDL',
            'hdl': 'HDL',
            'triglycerides': 'Triglycerides',
            'blood_pressure': 'Blood Pressure',
            'last_updated': 'Last Updated',
            'glucose_data': 'Glucose Data',
            'average': 'Average',
            'min': 'Min',
            'max': 'Max',
            'measurements': 'Measurements',
            'mg_dl': 'mg/dL',
            'percent': '%'
        }
    }
    t = labels.get(language, labels['zh'])
    
    # Patient info section: use shared builder when included, else empty (shown on main page instead)
    if include_patient_info:
        patient_info_html = build_patient_info_html(patient_info, patient_id, language, care_notes)
    else:
        patient_info_html = ""
    
    # Title based on language
    if language == 'zh':
        title = "AI食物日志总结 —— 赋能患者, 提升照护师效率"
    else:
        title = "AI Food Log Summary"
    
    # Food log summary section
    if language == 'zh':
        meal_order = ["早餐", "午餐", "晚餐", "其他"]
    else:
        meal_order = ["Breakfast", "Lunch", "Dinner", "Other"]
    
    food_log_html = '<div class="food-log-summary">'
    food_log_html += f'<h2>{t["food_log_summary"]}</h2>'
    
    # Check if there are any food logs at all
    # 检查是否有任何 food logs
    has_any_food_logs = False
    for meal_type in meal_order:
        if meal_type in food_logs_by_meal and food_logs_by_meal[meal_type]:
            has_any_food_logs = True
            break
    
    # If no food logs, show message
    # 如果没有 food logs，显示消息
    if not has_any_food_logs:
        food_log_html += f'<p style="color: #999; font-style: italic; padding: 20px; text-align: center;">{t["no_food_logs"]}</p>'
        food_log_html += '</div>'
        # Continue to build the rest of the HTML (patient info is already built)
        # 继续构建 HTML 的其余部分（病人信息已经构建）
    else:
        # Calculate total calories if possible
        # 如果可能，计算总热量
        total_calories_info = ""
        
        for meal_type in meal_order:
            if meal_type not in food_logs_by_meal or not food_logs_by_meal[meal_type]:
                continue
            
            food_log_html += f'<div class="meal-section">'
            
            # Collect all timestamps for this meal
            # Convert to PT timezone if timezone info is available, otherwise keep as-is
            meal_times = []
            pt_timezone = pytz.timezone('America/Los_Angeles')
            
            for row in food_logs_by_meal[meal_type]:
                # Try to get timestamp from various fields
                timestamp = None
                for field in ["createdAt", "created_at", "uploadedAt", "uploaded_at"]:
                    if field in row and pd.notna(row[field]):
                        try:
                            timestamp = pd.to_datetime(row[field])
                            break
                        except:
                            continue
                
                if timestamp is not None:
                    # If timestamp has timezone info, convert to PT
                    # If no timezone info, assume UTC and convert to PT
                    if timestamp.tzinfo is not None:
                        # Has timezone info, convert to PT
                        pt_timestamp = timestamp.astimezone(pt_timezone)
                    else:
                        # No timezone info, assume UTC and convert to PT
                        utc_timestamp = pytz.utc.localize(timestamp)
                        pt_timestamp = utc_timestamp.astimezone(pt_timezone)
                    meal_times.append(pt_timestamp)
            
            # Format meal header with times
            if meal_times:
                time_strs = []
                # Sort by time value
                sorted_times = sorted(set(meal_times))
                
                for pt_timestamp in sorted_times:
                    if language == 'zh':
                        time_str = pt_timestamp.strftime('%H:%M')  # 24-hour format for Chinese
                        time_str += ' PT'  # Always show PT since we always convert
                        time_strs.append(time_str)
                    else:
                        time_str = pt_timestamp.strftime('%I:%M %p')  # 12-hour format with AM/PM
                        time_str += ' PT'  # Always show PT since we always convert
                        time_strs.append(time_str)
                
                if language == 'zh':
                    times_display = f" ({t['logged_at']}: {', '.join(time_strs)})"
                else:
                    times_display = f" ({t['logged_at']}: {', '.join(time_strs)})"
            else:
                times_display = ""
            
            food_log_html += f'<h3>{meal_type}{times_display}</h3>'
            
            # Collect all images, ingredients, notes, comments, and meal summaries for this meal
            meal_images = []
            all_ingredients = []
            all_notes = []
            all_comments = []  # Store comments separately
            meal_summaries_list = []  # Store meal summaries for this meal
            
            for row in food_logs_by_meal[meal_type]:
                # Get food log ID
                # 获取 food log ID
                food_log_id = None
                for id_col in ["_id", "FoodLogId", "foodLogId"]:
                    if id_col in row and pd.notna(row[id_col]):
                        food_log_id = str(row[id_col]).strip()
                        break
                
                # Extract date from food log (from createdAt or Date field)
                # 从 food log 中提取日期（从 createdAt 或 Date 字段）
                food_log_date = date  # Default to the summary date
                if 'createdAt' in row and pd.notna(row['createdAt']):
                    try:
                        if isinstance(row['createdAt'], datetime):
                            food_log_date = row['createdAt'].strftime('%Y-%m-%d')
                        elif isinstance(row['createdAt'], str):
                            # Try to parse the date string
                            dt = datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00'))
                            food_log_date = dt.strftime('%Y-%m-%d')
                    except:
                        pass
                elif 'Date' in row and pd.notna(row['Date']):
                    try:
                        if isinstance(row['Date'], datetime):
                            food_log_date = row['Date'].strftime('%Y-%m-%d')
                        elif isinstance(row['Date'], str):
                            food_log_date = row['Date'][:10]  # Take YYYY-MM-DD part
                    except:
                        pass
                
                # Collect images - check for ImageURLs first (direct URLs from API)
                # 收集图片 - 首先检查 ImageURLs（来自 API 的直接 URL）
                image_urls = row.get("ImageURLs")
                if image_urls and isinstance(image_urls, list):
                    # Process each image URL
                    # 处理每个图片 URL
                    for image_index, img_url in enumerate(image_urls):
                        # If using data URI mode, download and convert
                        # 如果使用 data URI 模式，下载并转换
                        if use_data_uri:
                            local_path = download_image_with_cache(
                                img_url,
                                images_dir,
                                food_log_id=food_log_id,
                                image_index=image_index,
                                patient_id=patient_id,
                                date=food_log_date,
                                cache_db=cache_db,
                                debug=False
                            )
                            
                            if local_path and local_path.exists():
                                # Convert to data URI
                                # 转换为 data URI
                                try:
                                    ext = local_path.suffix.lower()
                                    mime = {
                                        ".jpg": "image/jpeg",
                                        ".jpeg": "image/jpeg",
                                        ".png": "image/png",
                                        ".gif": "image/gif",
                                        ".webp": "image/webp",
                                    }.get(ext, "image/jpeg")
                                    data = local_path.read_bytes()
                                    b64 = base64.b64encode(data).decode("ascii")
                                    data_uri = f"data:{mime};base64,{b64}"
                                    meal_images.append(data_uri)
                                except Exception as e:
                                    # If conversion fails, fall back to URL
                                    # 如果转换失败，回退到 URL
                                    meal_images.append(img_url)
                            else:
                                # Download failed, use URL directly
                                # 下载失败，直接使用 URL
                                meal_images.append(img_url)
                        else:
                            # Not using data URI, use URL directly
                            # 不使用 data URI，直接使用 URL
                            meal_images.append(img_url)
                        
                        # Collect meal summaries for these images
                        # 为这些图片收集 meal summaries
                        if meal_summaries and img_url in meal_summaries:
                            meal_summaries_list.append(meal_summaries[img_url])
                else:
                    # Fallback: check ImgName (might be URLs from API or local filenames)
                    img_names_str = str(row.get("ImgName", "") or "").strip()
                    img_items = [x.strip() for x in img_names_str.split(";") if x.strip()] if img_names_str else []
                    
                    for image_index, img_item in enumerate(img_items):
                        # Check if it's already a full URL (from API)
                        if img_item.startswith('http://') or img_item.startswith('https://'):
                            # Direct URL from API
                            # 如果是 data URI 模式，需要下载图片并转换为 data URI
                            # If data URI mode, need to download image and convert to data URI
                            if use_data_uri:
                                # Try to download with cache
                                # 尝试使用缓存下载
                                local_path = download_image_with_cache(
                                    img_item,
                                    images_dir,
                                    food_log_id=food_log_id,
                                    image_index=image_index,
                                    patient_id=patient_id,
                                    date=food_log_date,
                                    cache_db=cache_db,
                                    debug=False
                                )
                                
                                if local_path and local_path.exists():
                                    # Convert to data URI
                                    # 转换为 data URI
                                    try:
                                        ext = local_path.suffix.lower()
                                        mime = {
                                            ".jpg": "image/jpeg",
                                            ".jpeg": "image/jpeg",
                                            ".png": "image/png",
                                            ".gif": "image/gif",
                                            ".webp": "image/webp",
                                        }.get(ext, "image/jpeg")
                                        data = local_path.read_bytes()
                                        b64 = base64.b64encode(data).decode("ascii")
                                        data_uri = f"data:{mime};base64,{b64}"
                                        meal_images.append(data_uri)
                                    except Exception as e:
                                        # If conversion fails, fall back to URL
                                        # 如果转换失败，回退到 URL
                                        meal_images.append(img_item)
                                else:
                                    # Download failed, use URL directly
                                    # 下载失败，直接使用 URL
                                    meal_images.append(img_item)
                            else:
                                # Not using data URI, use URL directly
                                # 不使用 data URI，直接使用 URL
                                meal_images.append(img_item)
                            
                            # Collect meal summary if available
                            # 如果可用，收集 meal summary
                            if meal_summaries and img_item in meal_summaries:
                                meal_summaries_list.append(meal_summaries[img_item])
                        else:
                            # Local filename - only process if file exists (backward compatibility)
                            # 本地文件名 - 仅在文件存在时处理（向后兼容）
                            img_path = images_dir / img_item
                            if img_path.exists():
                                if use_data_uri:
                                    # Convert to data URI for embedding in HTML
                                    # 转换为 data URI 以嵌入 HTML
                                    try:
                                        ext = img_path.suffix.lower()
                                        mime = {
                                            ".jpg": "image/jpeg",
                                            ".jpeg": "image/jpeg",
                                            ".png": "image/png",
                                            ".gif": "image/gif",
                                            ".webp": "image/webp",
                                        }.get(ext, "image/jpeg")
                                        data = img_path.read_bytes()
                                        b64 = base64.b64encode(data).decode("ascii")
                                        data_uri = f"data:{mime};base64,{b64}"
                                        meal_images.append(data_uri)
                                    except Exception as e:
                                        # If conversion fails, fall back to URL
                                        # 如果转换失败，回退到 URL
                                        if image_base_url:
                                            meal_images.append(f"{image_base_url}/{img_item}")
                                        else:
                                            relative_path = f"{images_dir.name}/{img_item}"
                                            meal_images.append(relative_path)
                                else:
                                    # Use URL path
                                    # 使用 URL 路径
                                    if image_base_url:
                                        meal_images.append(f"{image_base_url}/{img_item}")
                                    else:
                                        relative_path = f"{images_dir.name}/{img_item}"
                                        meal_images.append(relative_path)
                
                # Collect ingredients
                ingredients_data = row.get("Ingredients") or row.get("ingredients")
                ingredients_summary = extract_ingredients_summary(ingredients_data)
                all_ingredients.extend(ingredients_summary["main_ingredients"])
                
                # Collect notes (description) - check multiple field names
                # 收集备注（描述）- 检查多个字段名
                description = (row.get("Description") or row.get("description") or 
                              row.get("note") or row.get("Note") or "")
                if description and pd.notna(description):
                    description_str = str(description).strip()
                    if description_str and description_str.lower() != "nan":
                        all_notes.append(description_str)
                
                # Collect RD/dietitian comments - check multiple possible field names
                # 收集营养师评论 - 检查多个可能的字段名
                comments_data = (row.get("comments") or row.get("Comments") or 
                               row.get("RD Comments") or row.get("rd_comments") or
                               row.get("rdComments") or row.get("comment"))
                if comments_data and pd.notna(comments_data):
                    # Check if it's a string that needs parsing, or already a dict/list
                    if isinstance(comments_data, str):
                        comments_str = comments_data.strip()
                        if comments_str and comments_str.lower() != "nan":
                            formatted_comments = format_rd_comments(comments_data, language)
                            all_comments.extend(formatted_comments)
                    else:
                        # Already a dict or list
                        formatted_comments = format_rd_comments(comments_data, language)
                        all_comments.extend(formatted_comments)
            
            # Display images
            if meal_images:
                food_log_html += '<div class="meal-images">'
                for img_url in meal_images:
                    # img_url can be either a direct HTTP URL or a data URI
                    food_log_html += f'<img src="{html.escape(img_url)}" alt="Food image" loading="lazy" />'
                food_log_html += '</div>'
            
            # Display ingredients
            if all_ingredients:
                food_log_html += '<div class="main-ingredients">'
                food_log_html += f'<div class="ingredients-label">{t["main_ingredients"]}:</div>'
                food_log_html += '<ul class="ingredients-list">'
                for ing in all_ingredients:
                    name = html.escape(str(ing.get("name", "")))
                    quantity = html.escape(str(ing.get("quantity", "")))
                    if name:
                        food_log_html += f'<li>{name}'
                        if quantity:
                            food_log_html += f' {quantity}'
                        food_log_html += '</li>'
                food_log_html += '</ul>'
                food_log_html += '</div>'
            
            # Display notes (patient's description)
            if all_notes:
                notes_label = "病人备注" if language == 'zh' else "Patient Notes"
                food_log_html += '<div class="meal-notes">'
                food_log_html += f'<div class="notes-label"><strong>{notes_label}:</strong></div>'
                for note in all_notes:
                    food_log_html += f'<p>{html.escape(note).replace(chr(10), "<br/>")}</p>'
                food_log_html += '</div>'
            
            # Display dietitian/RD comments
            if all_comments:
                comments_label = "营养师评论" if language == 'zh' else "Dietitian Comments"
                food_log_html += '<div class="meal-comments">'
                food_log_html += f'<div class="comments-label"><strong>{comments_label}:</strong></div>'
                for comment in all_comments:
                    comment_html = '<div class="comment-item">'
                    text = html.escape(comment.get("text", ""))
                    if text:
                        comment_html += f'<div class="comment-text">{text}</div>'
                    
                    # Show commented by info if available
                    commented_by = comment.get("commentedBy")
                    if isinstance(commented_by, dict):
                        name = commented_by.get("firstName") or commented_by.get("name") or ""
                        if name:
                            if commented_by.get("lastName"):
                                name += f" {commented_by.get('lastName')}"
                            commenter_title = commented_by.get("title") or ""
                            if commenter_title:
                                name += f" ({commenter_title})"
                            comment_html += f'<div class="comment-by">— {html.escape(name)}</div>'
                    elif isinstance(commented_by, str) and commented_by:
                        comment_html += f'<div class="comment-by">— {html.escape(commented_by)}</div>'
                    
                    # Show comment time if available (convert to PT timezone)
                    commented_at = comment.get("commentedAt")
                    if commented_at:
                        try:
                            # Try to parse and format the date, convert to PT timezone
                            dt = pd.to_datetime(commented_at)
                            # Convert to PT timezone
                            pt_timezone = pytz.timezone('America/Los_Angeles')
                            if dt.tzinfo is not None:
                                # Has timezone info, convert to PT
                                dt_pt = dt.astimezone(pt_timezone)
                            else:
                                # No timezone info, assume UTC and convert to PT
                                dt_utc = pytz.utc.localize(dt.to_pydatetime())
                                dt_pt = dt_utc.astimezone(pt_timezone)
                            
                            if language == 'zh':
                                time_str = dt_pt.strftime('%Y-%m-%d %H:%M PT')
                            else:
                                time_str = dt_pt.strftime('%Y-%m-%d %I:%M %p PT')
                            comment_html += f'<div class="comment-time">{time_str}</div>'
                        except:
                            comment_html += f'<div class="comment-time">{html.escape(str(commented_at))}</div>'
                    
                    comment_html += '</div>'
                    food_log_html += comment_html
                food_log_html += '</div>'
            
            # Display AI meal summaries (after RD comments)
            # 显示 AI meal summaries（在 RD comments 之后）
            if meal_summaries_list:
                summary_label = "AI 餐食分析" if language == 'zh' else "AI Meal Analysis"
                food_log_html += '<div class="meal-ai-summary">'
                food_log_html += f'<div class="ai-summary-label"><strong>{summary_label}:</strong></div>'
                
                for summary in meal_summaries_list:
                    summary_html = '<div class="ai-summary-item">'
                    
                    # AI Title
                    if summary.get("ai_title"):
                        summary_html += f'<div class="ai-title"><strong>{html.escape(summary["ai_title"])}</strong></div>'
                    
                    # Detected foods
                    if summary.get("detected_foods"):
                        foods_label = "检测到的食物" if language == 'zh' else "Detected Foods"
                        summary_html += f'<div class="ai-section"><strong>{foods_label}:</strong> '
                        foods_list = [html.escape(str(food)) for food in summary["detected_foods"]]
                        summary_html += ', '.join(foods_list)
                        summary_html += '</div>'
                    
                    # Composition
                    comp = summary.get("composition", {})
                    if any(comp.values()):
                        comp_label = "成分估计" if language == 'zh' else "Composition Estimate"
                        summary_html += f'<div class="ai-section"><strong>{comp_label}:</strong> '
                        comp_parts = []
                        if comp.get("carb") is not None:
                            comp_parts.append(f"碳水: {comp['carb']:.2f}" if language == 'zh' else f"Carbs: {comp['carb']:.2f}")
                        if comp.get("protein") is not None:
                            comp_parts.append(f"蛋白质: {comp['protein']:.2f}" if language == 'zh' else f"Protein: {comp['protein']:.2f}")
                        if comp.get("veg") is not None:
                            comp_parts.append(f"蔬菜: {comp['veg']:.2f}" if language == 'zh' else f"Vegetables: {comp['veg']:.2f}")
                        if comp.get("fat") is not None:
                            comp_parts.append(f"脂肪: {comp['fat']:.2f}" if language == 'zh' else f"Fat: {comp['fat']:.2f}")
                        summary_html += ', '.join(comp_parts)
                        summary_html += '</div>'
                    
                    # Observations
                    if summary.get("observations"):
                        obs_label = "观察" if language == 'zh' else "Observations"
                        summary_html += f'<div class="ai-section"><strong>{obs_label}:</strong> '
                        obs_list = []
                        for obs in summary["observations"]:
                            keyword = html.escape(str(obs.get("keyword", "")))
                            conf = obs.get("confidence", 1.0)
                            if conf < 1.0:
                                obs_list.append(f"{keyword} ({conf:.2f})")
                            else:
                                obs_list.append(keyword)
                        summary_html += ', '.join(obs_list)
                        summary_html += '</div>'
                    
                    # Dietary consideration
                    if summary.get("dietary_consideration"):
                        consider_label = "饮食考虑" if language == 'zh' else "Dietary Consideration"
                        summary_html += f'<div class="ai-section"><strong>{consider_label}:</strong> {html.escape(str(summary["dietary_consideration"]))}</div>'
                    
                    summary_html += '</div>'
                    food_log_html += summary_html
                
                food_log_html += '</div>'
            
            food_log_html += '</div>'  # meal-section
    
    food_log_html += '</div>'  # food-log-summary
    
    # Complete HTML
    html_content = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>食物日志总结 - {date}</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, Helvetica, Arial, 'Noto Sans', 'PingFang SC', 'Microsoft Yahei', sans-serif;
  background: #faf8f5;
  color: #1a1a1a;
  line-height: 1.6;
}}
.container {{
  max-width: 100%;
  width: 100%;
  margin: 0 auto;
  background: white;
  border-radius: 12px;
  padding: 40px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.08);
  box-sizing: border-box;
}}
.header {{
  text-align: center;
  margin-bottom: 40px;
  padding-bottom: 20px;
  border-bottom: 3px solid #e0e0e0;
}}
.header h1 {{
  margin: 0;
  font-size: 32px;
  font-weight: 700;
  color: #1a1a1a;
  letter-spacing: 1px;
}}
.content {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 40px;
  margin-top: 30px;
}}
.patient-info {{
  background: #f8f9fa;
  padding: 25px;
  border-radius: 10px;
  height: fit-content;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
  max-width: 100%;
}}
.patient-info h2 {{
  margin-top: 0;
  margin-bottom: 20px;
  font-size: 20px;
  font-weight: 600;
  color: #1a1a1a;
  border-bottom: 2px solid #4a90e2;
  padding-bottom: 12px;
}}
.patient-details {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.patient-details li {{
  padding: 10px 0;
  font-size: 15px;
  color: #333;
  border-bottom: 1px solid #e5e7eb;
  line-height: 1.8;
}}
.patient-details li:last-child {{
  border-bottom: none;
}}
.food-log-summary {{
  padding: 0;
  min-width: 0;
  width: 100%;
}}
.food-log-summary h2 {{
  margin-top: 0;
  margin-bottom: 25px;
  font-size: 24px;
  font-weight: 600;
  color: #1a1a1a;
  border-bottom: 2px solid #4a90e2;
  padding-bottom: 12px;
}}
.date-info {{
  margin: 0 0 30px 0;
  font-size: 15px;
  color: #666;
  font-weight: 500;
}}
.meal-section {{
  margin-bottom: 35px;
  padding: 25px;
  background: #fafafa;
  border-radius: 10px;
  border-left: 5px solid #4a90e2;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}}
.meal-section h3 {{
  margin-top: 0;
  margin-bottom: 20px;
  font-size: 20px;
  font-weight: 600;
  color: #1a1a1a;
}}
.meal-images {{
  display: flex;
  flex-wrap: wrap;
  gap: 15px;
  margin-bottom: 20px;
}}
.meal-images img {{
  max-width: 220px;
  max-height: 220px;
  width: auto;
  height: auto;
  object-fit: cover;
  border-radius: 10px;
  border: 2px solid #e5e7eb;
  box-shadow: 0 2px 6px rgba(0,0,0,0.1);
  transition: transform 0.2s;
}}
.meal-images img:hover {{
  transform: scale(1.05);
}}
.main-ingredients {{
  margin: 20px 0;
}}
.ingredients-label {{
  font-size: 15px;
  font-weight: 600;
  color: #1a1a1a;
  margin-bottom: 12px;
}}
.ingredients-list {{
  margin: 0;
  padding-left: 25px;
  list-style-type: disc;
}}
.ingredients-list li {{
  margin: 8px 0;
  font-size: 14px;
  color: #333;
  line-height: 1.6;
}}
.meal-notes {{
  margin-top: 20px;
  padding: 15px;
  background: #f0f7ff;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.8;
  color: #555;
  border-left: 3px solid #4a90e2;
}}
.notes-label {{
  font-weight: 600;
  margin-bottom: 10px;
  color: #1e40af;
  font-size: 14px;
}}
.meal-notes p {{
  margin: 8px 0;
}}
.meal-notes p:first-child {{
  margin-top: 0;
}}
.meal-notes p:last-child {{
  margin-bottom: 0;
}}
.meal-comments {{
  margin-top: 15px;
  padding: 12px;
  background: #fff9e6;
  border-left: 4px solid #ffc107;
  border-radius: 4px;
}}
.comments-label {{
  font-weight: 600;
  margin-bottom: 10px;
  color: #856404;
  font-size: 14px;
}}
.comment-item {{
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid #ffe082;
}}
.comment-item:last-child {{
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}}
.comment-text {{
  font-size: 14px;
  color: #333;
  line-height: 1.5;
  margin-bottom: 6px;
}}
.comment-by {{
  font-size: 12px;
  color: #856404;
  font-style: italic;
  margin-top: 4px;
}}
.comment-time {{
  font-size: 11px;
  color: #999;
  margin-top: 4px;
}}
.meal-ai-summary {{
  margin-top: 15px;
  padding: 15px;
  background: #f0f9ff;
  border-left: 4px solid #3b82f6;
  border-radius: 8px;
}}
.ai-summary-label {{
  font-weight: 600;
  margin-bottom: 12px;
  color: #1e40af;
  font-size: 15px;
}}
.ai-summary-item {{
  margin-bottom: 15px;
  padding: 12px;
  background: white;
  border-radius: 6px;
  border: 1px solid #dbeafe;
}}
.ai-summary-item:last-child {{
  margin-bottom: 0;
}}
.ai-title {{
  font-size: 15px;
  color: #1e40af;
  margin-bottom: 10px;
  font-weight: 600;
}}
.ai-section {{
  font-size: 13px;
  color: #333;
  line-height: 1.6;
  margin-bottom: 8px;
}}
.ai-section:last-child {{
  margin-bottom: 0;
}}
@media (max-width: 1024px) {{
  .content {{
    grid-template-columns: 1fr;
    gap: 30px;
  }}
  .patient-info {{
    order: 2;
  }}
  .food-log-summary {{
    order: 1;
  }}
}}
@media (max-width: 768px) {{
  .container {{
    padding: 20px;
  }}
  .header h1 {{
    font-size: 24px;
  }}
  .meal-images img {{
    max-width: 100%;
    max-height: 300px;
  }}
}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{title}</h1>
  </div>
  <div class="content">
    {patient_info_html}
    {food_log_html}
  </div>
</div>
</body>
</html>
"""
    
    return html_content


def generate_weekly_or_monthly_insight(
    patient_info: Dict[str, Any],
    patient_id: str,
    start_date: datetime,
    end_date: datetime,
    period_type: str,  # 'weekly' or 'monthly'
    openai_api_key: Optional[str] = None,
    prompt_file: Optional[Path] = None,
    language: str = 'zh',
    cache_db: Optional[CacheDB] = None,
    session_token: Optional[str] = None,
    regenerate: bool = False,
    debug: bool = False
) -> Optional[str]:
    """
    Generate weekly or monthly nutrition insight based on AI meal summaries.
    基于AI meal summaries生成周/月营养洞察。
    
    Args:
        patient_info: Patient information dictionary / 病人信息字典
        patient_id: Patient ID / 病人ID
        start_date: Start date of the period / 周期开始日期
        end_date: End date of the period / 周期结束日期
        period_type: 'weekly' or 'monthly' / 'weekly' 或 'monthly'
        openai_api_key: OpenAI API key / OpenAI API key
        prompt_file: Path to prompt file / prompt文件路径
        language: Language for output / 输出语言
        cache_db: Cache database instance / 缓存数据库实例
        session_token: Session token for API calls / API调用的会话令牌
        debug: Enable debug mode / 启用调试模式
        
    Returns:
        Generated insight text or None if failed / 生成的洞察文本或None
    """
    if OpenAI is None:
        if debug:
            print("[DEBUG] OpenAI package not available, skipping insight generation")
        return None
    
    # Get API key
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        if debug:
            print("[DEBUG] OPENAI_API_KEY not found, skipping insight generation")
        return None
    
    try:
        print(f"[INFO] Generating {period_type} insight for patient {patient_id} from {start_date} to {end_date}")
        
        # Check cache first (unless regenerate is True)
        # 首先检查缓存（除非 regenerate 为 True）
        # Cache is based on query date (end_date), not date range
        # 缓存基于查询日期（end_date），而不是日期范围
        # Note: regenerate only affects the period insight cache, not daily meal summaries
        # 注意：regenerate 只影响周期洞察缓存，不影响 daily meal summaries
        start_date_str = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
        end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
        
        if cache_db and not regenerate:
            # Cache key is based on end_date (query date) only
            # 缓存键仅基于 end_date（查询日期）
            cached_insight = cache_db.get_period_insight_cache(
                patient_id=patient_id,
                period_type=period_type,
                end_date=end_date_str,
                language=language
            )
            
            if cached_insight:
                print(f"[INFO] ✓ Using cached {period_type} insight for patient {patient_id} (query date: {end_date_str})")
                return cached_insight
            else:
                print(f"[INFO] ✗ Cache miss, generating new {period_type} insight for patient {patient_id} (query date: {end_date_str}, period: {start_date_str} to {end_date_str})")
        elif regenerate:
            print(f"[INFO] Regenerate mode: clearing {period_type} insight cache for patient {patient_id} (query date: {end_date_str})")
            # Remove only the specific period insight cache entry
            # 只移除特定的周期洞察缓存条目
            if cache_db:
                # The cache will be overwritten when we save the new insight
                # 当我们保存新的洞察时，缓存会被覆盖
                pass
        
        # Use the existing daily summary generation logic (which queries DB and uses cache)
        # 使用现有的daily summary生成逻辑（会查询数据库并使用缓存）
        from query_food_logs import query_food_logs
        
        # Get MongoDB client
        client = get_mongo_client()
        
        # Handle timezone-aware datetime for query
        if start_date.tzinfo is None:
            start_date_pt = pytz.timezone("America/Los_Angeles").localize(start_date)
        else:
            start_date_pt = start_date.astimezone(pytz.timezone("America/Los_Angeles"))
        
        if end_date.tzinfo is None:
            end_date_pt = pytz.timezone("America/Los_Angeles").localize(end_date)
        else:
            end_date_pt = end_date.astimezone(pytz.timezone("America/Los_Angeles"))
        
        # Convert to UTC for MongoDB query (as query_food_logs expects)
        start_date_utc = start_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
        end_date_utc = end_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
        
        print(f"[INFO] Querying food logs from {start_date_utc} to {end_date_utc}")
        
        # Query food logs using existing function
        food_logs_df = query_food_logs(
            client,
            [patient_id],
            database_name="UnifiedCare",
            start_date=start_date_utc,
            end_date=end_date_utc
        )
        
        print(f"[INFO] Found {len(food_logs_df)} food logs for the period")
        
        if food_logs_df.empty:
            print(f"[WARN] No food logs found for period {start_date} to {end_date}")
            return None
        
        # Get image URLs for all food logs first
        if session_token:
            print(f"[INFO] Fetching image URLs for {len(food_logs_df)} food logs...")
            food_logs_df, _ = get_food_log_image_urls(
                food_logs_df,
                session_token,
                debug=debug
            )
            print(f"[INFO] Image URLs fetched")
        else:
            print(f"[WARN] No session token provided, skipping image URL fetch")
        
        # Group by date and generate daily summaries
        daily_summaries = []
        # Convert timezone-aware datetime to date for comparison
        if hasattr(start_date_pt, 'date'):
            start_date_only = start_date_pt.date()
        elif hasattr(start_date, 'date'):
            start_date_only = start_date.date()
        else:
            start_date_only = start_date
        
        if hasattr(end_date_pt, 'date'):
            end_date_only = end_date_pt.date()
        elif hasattr(end_date, 'date'):
            end_date_only = end_date.date()
        else:
            end_date_only = end_date
        
        for date in pd.date_range(start=start_date_only, end=end_date_only, freq='D'):
            date_str = date.strftime('%Y-%m-%d')
            # Convert createdAt to date for comparison
            if 'createdAt' in food_logs_df.columns:
                try:
                    date_logs = food_logs_df[food_logs_df['createdAt'].dt.date == date.date()]
                except Exception as e:
                    if debug:
                        print(f"[DEBUG] Error filtering by date: {e}")
                    continue
            else:
                continue
            
            if date_logs.empty:
                continue
            
            # Group by meal
            food_logs_by_meal = group_food_logs_by_meal(date_logs, language=language)
            
            # Generate AI summaries for each meal if not cached
            daily_meal_summaries = []
            total_food_logs_processed = 0
            for meal_type, logs in food_logs_by_meal.items():
                for row in logs:
                    # Get image URLs from row (already fetched above)
                    image_urls = row.get("ImageURLs")
                    if not image_urls or not isinstance(image_urls, list):
                        # Fallback to ImgName if ImageURLs not available
                        img_names_str = str(row.get("ImgName", "") or "").strip()
                        if img_names_str:
                            image_urls = [url.strip() for url in img_names_str.split(";") 
                                        if url.strip() and (url.startswith('http://') or url.startswith('https://'))]
                        else:
                            image_urls = []
                    
                    if not image_urls:
                        continue
                    
                    total_food_logs_processed += 1
                    
                    # Get patient notes
                    patient_notes_text = None
                    description = row.get('Description') or row.get('note') or row.get('description')
                    if description and isinstance(description, str) and description.strip():
                        patient_notes_text = description.strip()
                    
                    # Get food_log_id
                    food_log_id = str(row.get('_id', '')) if hasattr(row.get('_id'), '__str__') else None
                    
                    # Analyze each image and download to cache
                    # Note: When generating weekly/monthly insight, we don't regenerate daily meal summaries
                    # even if regenerate=True, because regenerate should only affect the period insight cache
                    # 注意：生成 weekly/monthly insight 时，即使 regenerate=True，也不重新生成 daily meal summaries
                    # 因为 regenerate 应该只影响周期洞察缓存
                    for image_index, img_url in enumerate(image_urls):
                        # Download image to cache (if not already cached)
                        # 下载图片到缓存（如果还没有缓存）
                        images_dir = Path("./images")
                        local_path = download_image_with_cache(
                            img_url,
                            images_dir,
                            food_log_id=food_log_id,
                            image_index=image_index,
                            patient_id=patient_id,
                            date=date_str,
                            cache_db=cache_db,
                            debug=debug
                        )
                        
                        summary = analyze_food_image_with_openai(
                            img_url,
                            openai_api_key=api_key,
                            patient_notes=patient_notes_text,
                            food_log_id=food_log_id,
                            patient_id=patient_id,
                            date=date_str,
                            cache_db=cache_db,
                            regenerate=False,  # Always use cached daily meal summaries for period insights
                            debug=debug
                        )
                        if summary:
                            # Get image filename for Flask route
                            # 获取图片文件名用于 Flask 路由
                            image_filename = None
                            if local_path and local_path.exists():
                                image_filename = local_path.name
                            
                            daily_meal_summaries.append({
                                'date': date_str,
                                'meal_type': meal_type,
                                'summary': summary,
                                'image_url': img_url,  # Original URL for reference
                                'image_filename': image_filename  # Local filename for display
                            })
            
            if daily_meal_summaries:
                daily_summaries.append({
                    'date': date_str,
                    'summaries': daily_meal_summaries
                })
        
        # Count total food logs processed across all days
        total_food_logs = sum(len(daily['summaries']) for daily in daily_summaries)
        print(f"[INFO] Processed {total_food_logs} food log AI summaries across {len(daily_summaries)} days")
        
        if not daily_summaries:
            print("[WARN] No daily summaries generated")
            return None
        
        # Format summary text for prompt (include cached image paths and daily food variations)
        summary_text = ""
        # Store image references for post-processing
        image_references = []  # List of (placeholder, actual_path) tuples
        
        # For weekly insights, limit to 2-3 representative images
        # 对于 weekly insights，限制为 2-3 张代表性图片
        if period_type == 'weekly':
            # Select 2-3 representative images (one per day, prioritizing days with notable patterns)
            # 选择 2-3 张代表性图片（每天一张，优先选择有显著模式的日子）
            selected_images = []
            for daily in daily_summaries:
                # For each day, select the first meal with an image (or most representative)
                # 对于每一天，选择第一餐有图片的（或最有代表性的）
                for meal_summary in daily['summaries']:
                    image_filename = meal_summary.get('image_filename')
                    if image_filename:
                        selected_images.append({
                            'date': daily['date'],
                            'meal_type': meal_summary['meal_type'],
                            'image_filename': image_filename,
                            'summary': meal_summary['summary']
                        })
                        break  # Only one image per day
                # Limit to 3 images total
                if len(selected_images) >= 3:
                    break
        else:
            # For monthly, include all images (or more)
            # 对于 monthly，包含所有图片（或更多）
            selected_images = []
            for daily in daily_summaries:
                for meal_summary in daily['summaries']:
                    image_filename = meal_summary.get('image_filename')
                    if image_filename:
                        selected_images.append({
                            'date': daily['date'],
                            'meal_type': meal_summary['meal_type'],
                            'image_filename': image_filename,
                            'summary': meal_summary['summary']
                        })
        
        for daily in daily_summaries:
            summary_text += f"\n\nDate: {daily['date']}\n"
            # Collect all foods for this day to show daily variation
            daily_foods = []
            for meal_summary in daily['summaries']:
                meal_type = meal_summary['meal_type']
                summary = meal_summary['summary']
                image_filename = meal_summary.get('image_filename')
                
                summary_text += f"\nMeal: {meal_type}\n"
                
                # Only include image placeholder if this image is in the selected set
                # 只有当这张图片在选中集合中时才包含图片占位符
                is_selected = any(
                    img['date'] == daily['date'] and 
                    img['meal_type'] == meal_type and 
                    img['image_filename'] == image_filename
                    for img in selected_images
                )
                
                if image_filename and is_selected:
                    # Use placeholder that we'll replace later with actual image path
                    # 使用占位符，稍后替换为实际图片路径
                    placeholder = f"IMAGE_PLACEHOLDER_{len(image_references)}"
                    image_references.append((placeholder, image_filename))
                    # Provide clear instruction for AI to use the image selectively
                    # 为 AI 提供清晰的图片使用说明，要求选择性使用
                    if period_type == 'weekly':
                        summary_text += f"Food log image available for {meal_type} on {daily['date']}: Use this image ONLY if it helps illustrate a key point or notable pattern. Use Markdown syntax: ![Meal description]({placeholder})\n"
                    else:
                        summary_text += f"Food log image available for {meal_type} on {daily['date']}: Use this exact Markdown syntax in your response: ![Meal description]({placeholder})\n"
                summary_text += f"AI Title: {summary.get('ai_title', 'N/A')}\n"
                foods = summary.get('detected_foods', [])
                if foods:
                    summary_text += f"Foods: {', '.join(foods)}\n"
                    daily_foods.extend(foods)
                summary_text += f"Composition: {summary.get('composition', {})}\n"
                observations = summary.get('observations', [])
                if observations:
                    summary_text += f"Observations: {', '.join(observations)}\n"
            
            # Add daily food variation summary
            if daily_foods:
                from collections import Counter
                food_counts = Counter(daily_foods)
                summary_text += f"\nDaily Food Summary: {len(set(daily_foods))} unique foods, most frequent: {', '.join([f'{food}({count}x)' for food, count in food_counts.most_common(3)])}\n"
        
        # Load prompt template
        if prompt_file is None:
            if period_type == 'weekly':
                prompt_file = Path(__file__).parent / "weekly_nutrition_insight_prompt.txt"
            else:
                prompt_file = Path(__file__).parent / "monthly_nutrition_insight_prompt.txt"
        
        try:
            prompt_template = prompt_file.read_text(encoding="utf-8")
        except Exception as e:
            if debug:
                print(f"[DEBUG] Failed to read prompt file: {e}")
            return None
        
        # Format prompt
        try:
            start_date_str = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
            end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
            prompt_text = prompt_template.format(
                patient_id=patient_info.get('patient_id', patient_id),
                age=patient_info.get('age', 'N/A'),
                gender=patient_info.get('gender', 'N/A'),
                weight=patient_info.get('weight_kg', 'N/A'),
                height=patient_info.get('height_cm', 'N/A'),
                bmi=patient_info.get('bmi', 'N/A'),
                medical_history=patient_info.get('medical_history', 'N/A'),
                diagnoses=patient_info.get('diagnoses', 'N/A'),
                medications=patient_info.get('medications', 'N/A'),
                lab_results=patient_info.get('lab_results', 'N/A'),
                start_date=start_date_str,
                end_date=end_date_str,
                weekly_summary=summary_text if period_type == 'weekly' else '',
                monthly_summary=summary_text if period_type == 'monthly' else ''
            )
            print(f"[INFO] Prompt formatted successfully, length: {len(prompt_text)}")
        except Exception as e:
            print(f"[ERROR] Failed to format prompt: {e}")
            if debug:
                import traceback
                traceback.print_exc()
            return None
        
        # Add language instruction to prompt
        # 在 prompt 中添加语言指令
        if language == 'zh':
            # For Chinese, specify exact title format
            if period_type == 'weekly':
                prompt_text += f"\n\n请用简体中文回复。所有标题、标题和内容都必须是中文。主标题应为：## 周营养洞察 ({start_date_str} 至 {end_date_str})。所有小节标题（如'整体周评估'、'周营养模式'等）也必须是中文。"
            else:  # monthly
                prompt_text += f"\n\n请用简体中文回复。所有标题、标题和内容都必须是中文。主标题应为：## 月度营养洞察 ({start_date_str} 至 {end_date_str})。所有小节标题（如'整体月度评估'、'长期营养趋势'等）也必须是中文。"
        else:
            prompt_text += "\n\nPlease respond in English. All titles, headings, and content should be in English."
        
        # Call OpenAI
        print(f"[INFO] Calling OpenAI API to generate {period_type} insight...")
        try:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": prompt_text
                    }
                ],
                max_tokens=2000,
                temperature=0.7
            )
            
            insight_text = response.choices[0].message.content
            print(f"[INFO] Generated {period_type} insight: {len(insight_text)} characters")
            
            # Clean up any incorrect placeholder usage - do this comprehensively before replacement
            # 清理任何不正确的占位符使用 - 在替换之前全面清理
            import re
            for placeholder, image_filename in image_references if image_references else []:
                if not image_filename:
                    continue
                
                # Comprehensive cleanup: remove any file extensions or paths around the placeholder
                # 全面清理：移除占位符周围的任何文件扩展名或路径
                
                # Pattern 1: Remove extensions after placeholder (e.g., IMAGE_PLACEHOLDER_0.jpg)
                # 模式1：移除占位符后的扩展名（例如，IMAGE_PLACEHOLDER_0.jpg）
                pattern1 = re.escape(placeholder) + r'\.(jpg|jpeg|png|gif|webp)'
                insight_text = re.sub(pattern1, placeholder, insight_text)
                
                # Pattern 2: Remove paths/extensions before placeholder in parentheses (e.g., ![desc](xxx.jpgIMAGE_PLACEHOLDER_0))
                # 模式2：移除括号内占位符前的路径/扩展名（例如，![desc](xxx.jpgIMAGE_PLACEHOLDER_0)）
                pattern2 = r'\([^\)]*?[^\s\)]+\.(jpg|jpeg|png|gif|webp)' + re.escape(placeholder) + r'\)'
                insight_text = re.sub(pattern2, r'(' + placeholder + r')', insight_text)
                
                # Pattern 3: Remove paths/extensions before placeholder outside parentheses
                # 模式3：移除括号外占位符前的路径/扩展名
                pattern3 = r'[^\s\)]+\.(jpg|jpeg|png|gif|webp)' + re.escape(placeholder)
                insight_text = re.sub(pattern3, placeholder, insight_text)
            
            # Replace image placeholders with actual Flask routes
            # 将图片占位符替换为实际的 Flask 路由
            if image_references:
                replaced_count = 0
                for placeholder, image_filename in image_references:
                    if not image_filename:
                        if debug:
                            print(f"[DEBUG] Skipping placeholder {placeholder}: no image filename")
                        continue
                    
                    # Replace placeholder with Flask route path
                    # 将占位符替换为 Flask 路由路径
                    flask_image_path = f"/images/{image_filename}"
                    # Use regex to match placeholder
                    # 使用正则表达式匹配占位符
                    pattern = re.escape(placeholder)
                    if re.search(pattern, insight_text):
                        insight_text = re.sub(pattern, flask_image_path, insight_text)
                        replaced_count += 1
                        if debug:
                            print(f"[DEBUG] Replaced {placeholder} with {flask_image_path}")
                    elif debug:
                        print(f"[DEBUG] Placeholder {placeholder} not found in insight text")
                
                # Final validation: fix any incorrect paths that might have been generated
                # 最终验证：修复任何可能生成的错误路径
                # Check for patterns like /images/xxx.jpg0, /images/xxx.jpg1, etc.
                # 检查类似 /images/xxx.jpg0, /images/xxx.jpg1 等的模式
                incorrect_path_pattern = r'(/images/[^\s\)]+\.(jpg|jpeg|png|gif|webp))\d+'
                def fix_incorrect_path(match):
                    # Remove trailing digits after file extension
                    # 移除文件扩展名后的尾随数字
                    base_path = match.group(1)
                    if debug:
                        print(f"[DEBUG] Fixed incorrect path: {match.group(0)} -> {base_path}")
                    return base_path
                
                if re.search(incorrect_path_pattern, insight_text):
                    insight_text = re.sub(incorrect_path_pattern, fix_incorrect_path, insight_text)
                    if debug:
                        print(f"[DEBUG] Fixed incorrect image paths in insight text")
                
                print(f"[INFO] Replaced {replaced_count}/{len(image_references)} image placeholders with Flask routes for {period_type} insight")
            else:
                if debug:
                    print(f"[DEBUG] No image references found for {period_type} insight")
            
            # Save to cache
            # 保存到缓存
            # Cache is based on query date (end_date) only
            # 缓存仅基于查询日期（end_date）
            if cache_db and insight_text:
                start_date_str = start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else str(start_date)
                end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
                cache_db.save_period_insight_cache(
                    patient_id=patient_id,
                    period_type=period_type,
                    end_date=end_date_str,
                    insight_text=insight_text,
                    language=language,
                    start_date=start_date_str  # Stored for reference but not used in cache key
                )
                print(f"[INFO] Saved {period_type} insight to cache (query date: {end_date_str})")
            
            return insight_text
        except Exception as e:
            print(f"[ERROR] OpenAI API call failed: {e}")
            if debug:
                import traceback
                traceback.print_exc()
            return None
        
    except Exception as e:
        print(f"[ERROR] Error generating {period_type} insight: {e}")
        import traceback
        traceback.print_exc()
        return None


def generate_food_swapping_advice(
    patient_info: Dict[str, Any],
    patient_id: str,
    end_date: datetime,
    openai_api_key: Optional[str] = None,
    prompt_file: Optional[Path] = None,
    language: str = 'zh',
    cache_db: Optional[CacheDB] = None,
    session_token: Optional[str] = None,
    regenerate: bool = False,
    debug: bool = False
) -> Optional[str]:
    """
    Generate food swapping advice based on patient's food logs from the past week.
    基于病人过去一周的食物日志生成食物替换建议。
    
    Args:
        patient_info: Patient information dictionary / 病人信息字典
        patient_id: Patient ID / 病人ID
        end_date: End date (query date) / 结束日期（查询日期）
        openai_api_key: OpenAI API key / OpenAI API key
        prompt_file: Path to prompt file / prompt文件路径
        language: Language for output / 输出语言
        cache_db: Cache database instance / 缓存数据库实例
        session_token: Session token for API calls / API调用的会话令牌
        regenerate: Force regeneration / 强制重新生成
        debug: Enable debug mode / 启用调试模式
        
    Returns:
        Generated advice text or None if failed / 生成的建议文本或None
    """
    if OpenAI is None:
        if debug:
            print("[DEBUG] OpenAI package not available, skipping food swapping advice generation")
        return None
    
    # Get API key
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        if debug:
            print("[DEBUG] OPENAI_API_KEY not found, skipping food swapping advice generation")
        return None
    
    try:
        print(f"[INFO] Generating food swapping advice for patient {patient_id} (query date: {end_date})")
        
        # Check cache first (unless regenerate is True)
        # 首先检查缓存（除非 regenerate 为 True）
        # Cache is based on query date (end_date), not date range
        # 缓存基于查询日期（end_date），而不是日期范围
        end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
        
        if cache_db and not regenerate:
            # Use period_insight_cache with period_type='food_swapping'
            # 使用 period_insight_cache，period_type='food_swapping'
            cached_advice = cache_db.get_period_insight_cache(
                patient_id=patient_id,
                period_type='food_swapping',
                end_date=end_date_str,
                language=language
            )
            
            if cached_advice:
                print(f"[INFO] ✓ Using cached food swapping advice for patient {patient_id} (query date: {end_date_str})")
                return cached_advice
            else:
                print(f"[INFO] ✗ Cache miss, generating new food swapping advice for patient {patient_id} (query date: {end_date_str})")
        elif regenerate:
            print(f"[INFO] Regenerate mode: clearing food swapping advice cache for patient {patient_id} (query date: {end_date_str})")
            # The cache will be overwritten when we save the new advice
            # 当我们保存新的建议时，缓存会被覆盖
            pass
        
        # Calculate start date (7 days before end_date)
        # 计算开始日期（end_date 前7天）
        if hasattr(end_date, 'date'):
            end_date_only = end_date.date()
        else:
            end_date_only = end_date
        
        start_date_only = end_date_only - timedelta(days=7)
        start_date = datetime.combine(start_date_only, datetime.min.time())
        end_date_dt = datetime.combine(end_date_only, datetime.max.time())
        
        # Use the existing daily summary generation logic to get food logs and AI summaries
        # 使用现有的daily summary生成逻辑获取food logs和AI summaries
        from query_food_logs import query_food_logs
        
        # Get MongoDB client
        client = get_mongo_client()
        
        # Handle timezone-aware datetime for query
        import pytz
        if start_date.tzinfo is None:
            start_date_pt = pytz.timezone("America/Los_Angeles").localize(start_date)
        else:
            start_date_pt = start_date.astimezone(pytz.timezone("America/Los_Angeles"))
        
        if end_date_dt.tzinfo is None:
            end_date_pt = pytz.timezone("America/Los_Angeles").localize(end_date_dt)
        else:
            end_date_pt = end_date_dt.astimezone(pytz.timezone("America/Los_Angeles"))
        
        # Convert to UTC for MongoDB query
        start_date_utc = start_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
        end_date_utc = end_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
        
        print(f"[INFO] Querying food logs from {start_date_utc} to {end_date_utc} (past week)")
        
        # Query food logs
        food_logs_df = query_food_logs(
            client,
            [patient_id],
            database_name="UnifiedCare",
            start_date=start_date_utc,
            end_date=end_date_utc
        )
        
        print(f"[INFO] Found {len(food_logs_df)} food logs for the past week")
        
        if food_logs_df.empty:
            print(f"[WARN] No food logs found for the past week")
            return None
        
        # Get image URLs for all food logs
        if session_token:
            print(f"[INFO] Fetching image URLs for {len(food_logs_df)} food logs...")
            food_logs_df, _ = get_food_log_image_urls(
                food_logs_df,
                session_token,
                debug=debug
            )
            print(f"[INFO] Image URLs fetched")
        else:
            print(f"[WARN] No session token provided, skipping image URL fetch")
        
        # Collect all AI summaries from cache or generate if missing
        # 从缓存收集所有AI summaries，如果缺失则生成
        all_meal_summaries = []
        for _, row in food_logs_df.iterrows():
            # Get food log ID
            food_log_id = None
            for id_col in ["_id", "FoodLogId", "foodLogId"]:
                if id_col in row and pd.notna(row[id_col]):
                    food_log_id = str(row[id_col]).strip()
                    break
            
            # Get image URLs
            image_urls = row.get("ImageURLs")
            if not image_urls or not isinstance(image_urls, list):
                img_names_str = str(row.get("ImgName", "") or "").strip()
                if img_names_str:
                    image_urls = [url.strip() for url in img_names_str.split(";") 
                                if url.strip() and (url.startswith('http://') or url.startswith('https://'))]
            
            # Extract patient notes
            patient_notes_text = None
            if 'Description' in row and pd.notna(row['Description']):
                patient_notes_text = str(row['Description']).strip()
            elif 'note' in row and pd.notna(row['note']):
                patient_notes_text = str(row['note']).strip()
            elif 'description' in row and pd.notna(row['description']):
                patient_notes_text = str(row['description']).strip()
            
            # Extract date
            food_log_date = None
            if 'createdAt' in row and pd.notna(row['createdAt']):
                try:
                    if isinstance(row['createdAt'], datetime):
                        food_log_date = row['createdAt'].strftime('%Y-%m-%d')
                    elif isinstance(row['createdAt'], str):
                        dt = datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00'))
                        food_log_date = dt.strftime('%Y-%m-%d')
                except:
                    pass
            elif 'Date' in row and pd.notna(row['Date']):
                try:
                    if isinstance(row['Date'], datetime):
                        food_log_date = row['Date'].strftime('%Y-%m-%d')
                    elif isinstance(row['Date'], str):
                        food_log_date = row['Date'][:10]
                except:
                    pass
            
            # Get meal type
            meal_type = row.get('MealType', 'Other')
            if pd.isna(meal_type):
                meal_type = 'Other'
            
            # Analyze first image to get AI summary and download image to cache
            if image_urls:
                for image_index, img_url in enumerate(image_urls[:1]):  # Analyze first image only
                    # Download image to cache (if not already cached)
                    # 下载图片到缓存（如果还没有缓存）
                    images_dir = Path("./images")
                    local_path = download_image_with_cache(
                        img_url,
                        images_dir,
                        food_log_id=food_log_id,
                        image_index=image_index,
                        patient_id=patient_id,
                        date=food_log_date,
                        cache_db=cache_db,
                        debug=debug
                    )
                    
                    summary = analyze_food_image_with_openai(
                        img_url,
                        openai_api_key=api_key,
                        patient_notes=patient_notes_text,
                        food_log_id=food_log_id,
                        patient_id=patient_id,
                        date=food_log_date,
                        cache_db=cache_db,
                        regenerate=False,  # Always use cache or generate if missing, don't force regeneration
                        debug=debug
                    )
                    if summary:
                        # Get image filename for Flask route
                        # 获取图片文件名用于 Flask 路由
                        image_filename = None
                        if local_path and local_path.exists():
                            image_filename = local_path.name
                        
                        all_meal_summaries.append({
                            'date': food_log_date,
                            'meal_type': meal_type,
                            'summary': summary,
                            'image_url': img_url,  # Original URL for reference
                            'image_filename': image_filename  # Local filename for display
                        })
                    break
        
        client.close()
        
        if not all_meal_summaries:
            print(f"[WARN] No AI summaries available for food swapping advice")
            return None
        
        # Analyze frequent foods by meal type
        # 按餐类型分析经常吃的食物
        from collections import Counter
        frequent_foods_by_meal = {}
        for entry in all_meal_summaries:
            meal_type = entry['meal_type']
            summary = entry['summary']
            
            # Extract food items from summary
            food_items = []
            if 'ai_title' in summary:
                food_items.append(summary['ai_title'])
            if 'food_items' in summary and isinstance(summary['food_items'], list):
                food_items.extend(summary['food_items'])
            elif 'food_items' in summary and isinstance(summary['food_items'], str):
                food_items.append(summary['food_items'])
            
            if meal_type not in frequent_foods_by_meal:
                frequent_foods_by_meal[meal_type] = []
            frequent_foods_by_meal[meal_type].extend(food_items)
        
        # Count frequency
        frequent_foods_summary = {}
        for meal_type, foods in frequent_foods_by_meal.items():
            counter = Counter(foods)
            frequent_foods_summary[meal_type] = dict(counter.most_common(10))
        
        # Format frequent foods summary for prompt (include image references)
        frequent_foods_text = ""
        # Store image references for post-processing
        image_references = []  # List of (placeholder, image_filename) tuples
        
        for meal_type, foods in frequent_foods_summary.items():
            frequent_foods_text += f"\n{meal_type}:\n"
            for food, count in foods.items():
                frequent_foods_text += f"  - {food} (appeared {count} time(s))"
                
                # Find corresponding image for this food
                # 找到这个食物对应的图片
                for entry in all_meal_summaries:
                    if entry['meal_type'] == meal_type:
                        summary = entry['summary']
                        # Check if this food matches the entry
                        food_matches = False
                        if 'ai_title' in summary and food.lower() in summary['ai_title'].lower():
                            food_matches = True
                        elif 'food_items' in summary:
                            if isinstance(summary['food_items'], list):
                                food_matches = any(food.lower() in str(item).lower() for item in summary['food_items'])
                            elif isinstance(summary['food_items'], str):
                                food_matches = food.lower() in summary['food_items'].lower()
                        
                        if food_matches and entry.get('image_filename'):
                            # Use placeholder that we'll replace later with actual image path
                            # 使用占位符，稍后替换为实际图片路径
                            placeholder = f"IMAGE_PLACEHOLDER_{len(image_references)}"
                            image_references.append((placeholder, entry['image_filename']))
                            # Just add placeholder without text description to avoid line break issues
                            # 只添加占位符，不添加文字说明，避免换行问题
                            frequent_foods_text += f" {placeholder}"
                            break
                
                frequent_foods_text += "\n"
        
        # Prepare patient information for prompt
        patient_background = patient_info.get('background', 'Not specified')
        medical_history = patient_info.get('medicalHistory', 'Not specified')
        nutritional_goals = patient_info.get('nutritionalGoals', 'Not specified')
        nutritional_considerations = patient_info.get('nutritionalConsiderations', 'Not specified')
        dietary_preferences = patient_info.get('dietaryPreferences', 'Not specified')
        cultural_background = patient_info.get('culturalBackground', 'Not specified')
        age = patient_info.get('age', 'Not specified')
        
        # Load prompt file
        if prompt_file is None:
            prompt_file = Path(__file__).parent / "food_swapping_advice_prompt.txt"
        
        try:
            prompt_text = prompt_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"[ERROR] Prompt file not found: {prompt_file}")
            return None
        
        # Format prompt with patient information
        prompt_text = prompt_text.format(
            patient_background=patient_background,
            medical_history=medical_history,
            nutritional_goals=nutritional_goals,
            nutritional_considerations=nutritional_considerations,
            dietary_preferences=dietary_preferences,
            cultural_background=cultural_background,
            age=age,
            frequent_foods_summary=frequent_foods_text
        )
        
        # Add language instruction
        if language == 'zh':
            prompt_text += "\n\nPlease respond in Chinese (Simplified)."
        else:
            prompt_text += "\n\nPlease respond in English."
        
        # Call OpenAI
        print(f"[INFO] Calling OpenAI API to generate food swapping advice...")
        try:
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": prompt_text
                    }
                ],
                max_tokens=2000,
                temperature=0.7
            )
            
            advice_text = response.choices[0].message.content
            print(f"[INFO] Generated food swapping advice: {len(advice_text)} characters")
            
            # Clean up any incorrect placeholder usage - do this comprehensively before replacement
            # 清理任何不正确的占位符使用 - 在替换之前全面清理
            import re
            for placeholder, image_filename in image_references if image_references else []:
                if not image_filename:
                    continue
                
                # Comprehensive cleanup: remove any file extensions or paths around the placeholder
                # 全面清理：移除占位符周围的任何文件扩展名或路径
                
                # Pattern 1: Remove extensions after placeholder (e.g., IMAGE_PLACEHOLDER_0.jpg)
                # 模式1：移除占位符后的扩展名（例如，IMAGE_PLACEHOLDER_0.jpg）
                pattern1 = re.escape(placeholder) + r'\.(jpg|jpeg|png|gif|webp)'
                advice_text = re.sub(pattern1, placeholder, advice_text)
                
                # Pattern 2: Remove paths/extensions before placeholder in parentheses (e.g., ![desc](xxx.jpgIMAGE_PLACEHOLDER_0))
                # 模式2：移除括号内占位符前的路径/扩展名（例如，![desc](xxx.jpgIMAGE_PLACEHOLDER_0)）
                pattern2 = r'\([^\)]*?[^\s\)]+\.(jpg|jpeg|png|gif|webp)' + re.escape(placeholder) + r'\)'
                advice_text = re.sub(pattern2, r'(' + placeholder + r')', advice_text)
                
                # Pattern 3: Remove paths/extensions before placeholder outside parentheses
                # 模式3：移除括号外占位符前的路径/扩展名
                pattern3 = r'[^\s\)]+\.(jpg|jpeg|png|gif|webp)' + re.escape(placeholder)
                advice_text = re.sub(pattern3, placeholder, advice_text)
            
            # Replace image placeholders with actual Flask routes
            # 将图片占位符替换为实际的 Flask 路由
            if image_references:
                replaced_count = 0
                for placeholder, image_filename in image_references:
                    if not image_filename:
                        if debug:
                            print(f"[DEBUG] Skipping placeholder {placeholder}: no image filename")
                        continue
                    
                    # Replace placeholder with Flask route path
                    # 将占位符替换为 Flask 路由路径
                    flask_image_path = f"/images/{image_filename}"
                    # Use regex to match placeholder
                    # 使用正则表达式匹配占位符
                    pattern = re.escape(placeholder)
                    if re.search(pattern, advice_text):
                        advice_text = re.sub(pattern, flask_image_path, advice_text)
                        replaced_count += 1
                        if debug:
                            print(f"[DEBUG] Replaced {placeholder} with {flask_image_path}")
                    elif debug:
                        print(f"[DEBUG] Placeholder {placeholder} not found in advice text")
                
                # Final validation: fix any incorrect paths that might have been generated
                # 最终验证：修复任何可能生成的错误路径
                # Check for patterns like /images/xxx.jpg0, /images/xxx.jpg1, etc.
                # 检查类似 /images/xxx.jpg0, /images/xxx.jpg1 等的模式
                incorrect_path_pattern = r'(/images/[^\s\)]+\.(jpg|jpeg|png|gif|webp))\d+'
                def fix_incorrect_path(match):
                    # Remove trailing digits after file extension
                    # 移除文件扩展名后的尾随数字
                    base_path = match.group(1)
                    if debug:
                        print(f"[DEBUG] Fixed incorrect path: {match.group(0)} -> {base_path}")
                    return base_path
                
                if re.search(incorrect_path_pattern, advice_text):
                    advice_text = re.sub(incorrect_path_pattern, fix_incorrect_path, advice_text)
                    if debug:
                        print(f"[DEBUG] Fixed incorrect image paths in advice text")
                
                print(f"[INFO] Replaced {replaced_count}/{len(image_references)} image placeholders with Flask routes")
            
            # Save to cache
            # 保存到缓存
            # Cache is based on query date (end_date) only
            # 缓存仅基于查询日期（end_date）
            if cache_db and advice_text:
                end_date_str = end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else str(end_date)
                cache_db.save_period_insight_cache(
                    patient_id=patient_id,
                    period_type='food_swapping',
                    end_date=end_date_str,
                    insight_text=advice_text,
                    language=language,
                    start_date=end_date_str  # Stored for reference but not used in cache key
                )
                print(f"[INFO] Saved food swapping advice to cache (query date: {end_date_str})")
            
            return advice_text
        except Exception as e:
            print(f"[ERROR] OpenAI API call failed: {e}")
            if debug:
                import traceback
                traceback.print_exc()
            return None
        
    except Exception as e:
        print(f"[ERROR] Error generating food swapping advice: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Generate food log summary for a specific patient on a specific date. / 为特定病人在特定日期生成食物日志总结。"
    )
    
    parser.add_argument(
        "--patient-id",
        required=True,
        help="Patient ID / 病人ID"
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Date in YYYY-MM-DD format / 日期格式 YYYY-MM-DD"
    )
    parser.add_argument(
        "--mongo-uri",
        default=None,
        help="MongoDB connection URI (or set MONGO_DATABASE_URI env var) / MongoDB连接URI"
    )
    parser.add_argument(
        "--database",
        default="UnifiedCare",
        help="Database name / 数据库名称"
    )
    parser.add_argument(
        "--session-token",
        default=None,
        help="Session token for downloading images (or set SESSION_TOKEN env var) / 下载图片的会话令牌"
    )
    parser.add_argument(
        "--images-dir",
        default="./images",
        help="Images directory / 图片目录"
    )
    parser.add_argument(
        "--output",
        default="food_log_summary.html",
        help="Output HTML file / 输出HTML文件"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode to show detailed logs including FoodLog IDs / 启用调试模式显示详细日志包括FoodLog ID"
    )
    
    args = parser.parse_args()
    
    # Parse date (assuming input date is in PT timezone)
    # Convert to UTC for MongoDB query since MongoDB stores dates in UTC
    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
        # Assume the input date is in PT timezone
        pt_timezone = pytz.timezone('America/Los_Angeles')
        # Create start and end of day in PT timezone
        start_date_pt = pt_timezone.localize(target_date.replace(hour=0, minute=0, second=0, microsecond=0))
        end_date_pt = pt_timezone.localize(target_date.replace(hour=23, minute=59, second=59, microsecond=999999))
        # Convert to UTC for MongoDB query
        start_date = start_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
        end_date = end_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
    except ValueError:
        print(f"[ERROR] Invalid date format. Please use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(1)
    
    # Get session token
    session_token = args.session_token or os.getenv("SESSION_TOKEN")
    if not session_token:
        print("[WARN] No session token provided. Image download will be skipped.", file=sys.stderr)
        print("[WARN] Set SESSION_TOKEN environment variable or use --session-token option.", file=sys.stderr)
    
    # Connect to MongoDB
    print("[INFO] Connecting to MongoDB...")
    try:
        client = get_mongo_client(args.mongo_uri)
        print("[OK] MongoDB connection successful")
    except Exception as e:
        print(f"[ERROR] Failed to connect to MongoDB: {e}", file=sys.stderr)
        sys.exit(1)
    
    try:
        # Get patient info
        print(f"[INFO] Fetching patient info for {args.patient_id}...")
        patient_info = get_patient_info(client, args.patient_id, args.database)
        
        # Query food logs
        print(f"[INFO] Querying food logs for patient {args.patient_id} on {args.date}...")
        food_logs_df = query_food_logs(
            client,
            [args.patient_id],
            database_name=args.database,
            start_date=start_date,
            end_date=end_date
        )
        
        if food_logs_df.empty:
            print(f"[WARN] No food logs found for patient {args.patient_id} on {args.date}")
            client.close()
            sys.exit(0)
        
        print(f"[OK] Found {len(food_logs_df)} food log entries")
        
        # Setup images directory
        images_dir = Path(args.images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)
        
        # Get image URLs from API if session token is provided
        api_responses = {}
        if session_token:
            print("[INFO] Getting image URLs from API...")
            if args.debug:
                print("[DEBUG] Debug mode: ON - will show detailed FoodLog ID information")
            food_logs_df, api_responses = get_food_log_image_urls(
                food_logs_df,
                session_token,
                debug=args.debug
            )
        
        # Group by meal type
        food_logs_by_meal = group_food_logs_by_meal(food_logs_df)
        print(f"[INFO] Grouped into: {', '.join(food_logs_by_meal.keys())}")
        
        # Generate HTML
        print("[INFO] Generating HTML summary...")
        html_content = generate_html_summary(
            patient_info,
            food_logs_by_meal,
            images_dir,
            args.date,
            args.patient_id
        )
        
        # Write output
        output_path = Path(args.output)
        output_path.write_text(html_content, encoding="utf-8")
        print(f"[OK] Generated: {output_path.resolve()}")
        
    except Exception as e:
        print(f"[ERROR] Failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
