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
        
        # Generate filename from food_log_id + image_index (preferred) or URL hash (fallback)
        # 从 food_log_id + image_index（优先）或 URL 哈希（备用）生成文件名
        if food_log_id is not None and image_index is not None:
            # Use food_log_id for naming
            # 使用 food_log_id 命名
            ext = guess_ext_from_url(image_url)
            if image_index > 0:
                filename = f"{food_log_id}_{image_index}{ext}"
            else:
                filename = f"{food_log_id}{ext}"
        else:
            # Fallback to URL hash
            # 回退到 URL 哈希
            import hashlib
            url_hash = hashlib.md5(image_url.encode('utf-8')).hexdigest()
            ext = guess_ext_from_url(image_url)
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
    cache_db: Optional[CacheDB] = None,
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
    
    # Check cache first
    # 首先检查缓存
    if cache_db:
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
                print(f"[INFO] ✓ Using cached AI summary for food_log_id: {food_log_id or 'N/A'}")
            return cached_summary
        else:
            if debug:
                print(f"[DEBUG] ✗ Cache MISS")
            else:
                print(f"[INFO] ✗ Cache miss, generating new AI summary for food_log_id: {food_log_id or 'N/A'}")
    
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
    cache_db: Optional[CacheDB] = None
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
    
    # Debug: Print what patient_info contains
    print(f"[DEBUG] generate_html_summary: patient_info keys = {list(patient_info.keys())}")
    print(f"[DEBUG] generate_html_summary: patient_info = {patient_info}")
    
    # Patient info section
    # Always show at least patient ID
    patient_info_html = f"""
    <div class="patient-info">
        <h2>{t['patient_info']}</h2>
        <ul class="patient-details">
    """
    
    # Always show patient ID
    patient_id_display = patient_info.get("patient_id") or patient_id
    patient_id_label = "患者ID" if language == 'zh' else "Patient ID"
    patient_info_html += f'<li><strong>{patient_id_label}:</strong> {html.escape(str(patient_id_display))}</li>'
    
    # Show name if available
    if patient_info.get("name"):
        name_label = "姓名" if language == 'zh' else "Name"
        patient_info_html += f'<li><strong>{name_label}:</strong> {html.escape(str(patient_info["name"]))}</li>'
    
    # Show individual fields (always show all extracted fields)
    # 显示各个字段（总是显示所有提取到的字段）
    # Use more lenient checks to ensure data is displayed
    # 使用更宽松的检查条件确保数据显示
    
    # Age
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
    
    # Weight and Height
    weight_val = patient_info.get("weight")
    height_val = patient_info.get("height")
    if weight_val is not None and weight_val != "" and height_val is not None and height_val != "":
        patient_info_html += f'<li><strong>{t["weight"]}:</strong> {weight_val}kg, <strong>{t["height"]}:</strong> {height_val}cm</li>'
    elif weight_val is not None and weight_val != "":
        patient_info_html += f'<li><strong>{t["weight"]}:</strong> {weight_val}kg</li>'
    elif height_val is not None and height_val != "":
        patient_info_html += f'<li><strong>{t["height"]}:</strong> {height_val}cm</li>'
    
    # BMI
    bmi_val = patient_info.get("bmi")
    if bmi_val is not None and bmi_val != "":
        patient_info_html += f'<li><strong>BMI:</strong> {bmi_val}</li>'
    
    # Medical history (from healthConditions)
    medical_history = patient_info.get("medical_history")
    if medical_history and str(medical_history).strip():
        patient_info_html += f'<li><strong>{t["medical_history"]}:</strong> {html.escape(str(medical_history))}</li>'
    
    # Diagnoses (from diagnoses_display)
    diagnoses = patient_info.get("diagnoses")
    if diagnoses and str(diagnoses).strip():
        diagnoses_label = "诊断" if language == 'zh' else "Diagnoses"
        patient_info_html += f'<li><strong>{diagnoses_label}:</strong> {html.escape(str(diagnoses))}</li>'
    
    # Ethnicity
    ethnicity = patient_info.get("ethnicity")
    if ethnicity and str(ethnicity).strip():
        patient_info_html += f'<li><strong>{t["ethnicity"]}:</strong> {html.escape(str(ethnicity))}</li>'
    
    # Region
    region = patient_info.get("region")
    if region and str(region).strip():
        patient_info_html += f'<li><strong>{t["region"]}:</strong> {html.escape(str(region))}</li>'
    
    # Exercise intensity
    exercise = patient_info.get("exercise_intensity")
    if exercise and str(exercise).strip():
        patient_info_html += f'<li><strong>{t["exercise_intensity"]}:</strong> {html.escape(str(exercise))}</li>'
    
    # Medications
    medications = patient_info.get("medications")
    if medications and str(medications).strip():
        patient_info_html += f'<li><strong>{t["medications"]}:</strong> {html.escape(str(medications))}</li>'
    
    # If no additional info found, show a message
    if len(patient_info) == 1:  # Only patient_id
        no_info_msg = "暂无其他患者信息" if language == 'zh' else "No additional patient information available"
        patient_info_html += f'<li style="color: #999; font-style: italic;">{no_info_msg}</li>'
    
    patient_info_html += """
        </ul>
    """
    
    # Add care notes section if available
    # 如果可用，添加 care notes 部分
    if care_notes and len(care_notes) > 0:
        care_notes_label = "Care Notes" if language == 'en' else "Care Notes"
        view_details_text = "查看详情" if language == 'zh' else "View Details"
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">{care_notes_label}</h3>
            <div style="max-height: 300px; overflow-y: auto;">
        """
        
        for index, note in enumerate(care_notes):
            # Get full note text for summary
            full_note_text = ''
            if note.get('note'):
                full_note_text = note['note']
            elif note.get('content'):
                full_note_text = note['content']
            elif note.get('text'):
                full_note_text = note['text']
            else:
                # Show all fields if no specific note field
                fields = []
                for key, value in note.items():
                    if key not in ['_id', 'memberId', 'patient_id']:
                        if isinstance(value, (dict, list)):
                            fields.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
                        else:
                            fields.append(f"{key}: {value}")
                full_note_text = ', '.join(fields) if fields else 'No content'
            
            # Create summary (first 100 characters)
            summary = full_note_text[:100] + '...' if len(full_note_text) > 100 else full_note_text
            
            # Format date if available
            date_str = ''
            if note.get('createdAt'):
                try:
                    if isinstance(note['createdAt'], str):
                        date_obj = datetime.fromisoformat(note['createdAt'].replace('Z', '+00:00'))
                    else:
                        date_obj = note['createdAt']
                    date_str = date_obj.strftime('%Y-%m-%d')
                except:
                    date_str = str(note.get('createdAt', ''))
            
            # Create link to view full note
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
        # Show message if no care notes
        no_care_notes_text = "暂无Care Notes" if language == 'zh' else "No Care Notes"
        patient_info_html += f"""
        <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e0e0e0;">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 16px; color: #2c3e50;">Care Notes</h3>
            <div style="color: #999; font-style: italic;">{no_care_notes_text}</div>
        </div>
        """
    
    patient_info_html += """
    </div>
    """
    
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
  max-width: 1400px;
  margin: 0 auto;
  background: white;
  border-radius: 12px;
  padding: 40px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.08);
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
  grid-template-columns: 350px 1fr;
  gap: 40px;
  margin-top: 30px;
}}
.patient-info {{
  background: #f8f9fa;
  padding: 25px;
  border-radius: 10px;
  height: fit-content;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
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
