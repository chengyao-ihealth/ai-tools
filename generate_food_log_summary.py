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


def get_patient_info(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare") -> Dict[str, Any]:
    """
    Get simple patient background information from database.
    If a patient info summary exists, use it directly.
    从数据库获取简单的患者背景信息。如果有现成的patient info summary就直接用。
    
    Args:
        client: MongoDB client
        patient_id: Patient ID
        database_name: Database name
        
    Returns:
        Dict with patient information (always includes patient_id)
    """
    db = client[database_name]
    patient_info = {"patient_id": patient_id}  # Always include patient_id
    
    # Try to get from patients or members collection
    # 尝试从patients或members集合获取
    for collection_name in ["patients", "members", "uc_enrolled_programs"]:
        try:
            collection = db[collection_name]
            
            # Try different ID fields
            for id_field in ["_id", "patient_id", "memberId", "member_id", "id"]:
                try:
                    # Convert patient_id to ObjectId if possible
                    query_id = patient_id
                    if id_field == "_id" or id_field == "memberId" or id_field == "id":
                        try:
                            query_id = ObjectId(patient_id)
                        except Exception:
                            pass
                    
                    query = {id_field: query_id}
                    doc = collection.find_one(query)
                    
                    if doc:
                        print(f"[INFO] Found document in {collection_name} collection with field {id_field}")
                        
                        # Check for existing patient info summary first
                        # 先检查是否有现成的patient info summary
                        for summary_field in ["patient_info_summary", "patientInfoSummary", "patient_info", "patientInfo", "summary", "background"]:
                            if summary_field in doc and doc[summary_field]:
                                patient_info["summary"] = str(doc[summary_field])
                                print(f"[INFO] Found patient info summary from {collection_name} collection (field: {summary_field})")
                                return patient_info
                        
                        # Extract basic patient background information
                        # 尝试更多的字段名称变体
                        # 提取基本的患者背景信息
                        # Age - 尝试多种字段名
                        age = (doc.get("age") or doc.get("Age") or doc.get("birthYear") or 
                              (doc.get("birthDate") and (datetime.now().year - pd.to_datetime(doc["birthDate"]).year) if pd.notna(doc.get("birthDate")) else None))
                        if age:
                            try:
                                patient_info["age"] = int(age) if isinstance(age, (int, float)) else age
                            except:
                                pass
                        
                        # Gender
                        gender = (doc.get("gender") or doc.get("Gender") or doc.get("sex") or 
                                 doc.get("Sex") or doc.get("性别"))
                        if gender:
                            patient_info["gender"] = str(gender)
                        
                        # Weight
                        weight = (doc.get("weight") or doc.get("Weight") or doc.get("weight_kg") or 
                                 doc.get("weightKg") or doc.get("体重"))
                        if weight:
                            patient_info["weight"] = weight
                        
                        # Height
                        height = (doc.get("height") or doc.get("Height") or doc.get("height_cm") or 
                                 doc.get("heightCm") or doc.get("身高"))
                        if height:
                            patient_info["height"] = height
                        
                        # BMI
                        bmi = doc.get("bmi") or doc.get("BMI") or doc.get("bodyMassIndex")
                        if bmi:
                            patient_info["bmi"] = bmi
                        
                        # Medical history
                        medical_history = (doc.get("medical_history") or doc.get("medicalHistory") or 
                                         doc.get("disease_history") or doc.get("diseaseHistory") or
                                         doc.get("conditions") or doc.get("Conditions") or
                                         doc.get("diagnosis") or doc.get("Diagnosis"))
                        if medical_history:
                            patient_info["medical_history"] = str(medical_history)
                        
                        # Ethnicity
                        ethnicity = (doc.get("ethnicity") or doc.get("Ethnicity") or 
                                    doc.get("民族") or doc.get("race") or doc.get("Race"))
                        if ethnicity:
                            patient_info["ethnicity"] = str(ethnicity)
                        
                        # Region/Location
                        region = (doc.get("region") or doc.get("Region") or 
                                 doc.get("地域") or doc.get("location") or doc.get("Location") or
                                 doc.get("address") or doc.get("Address") or
                                 doc.get("city") or doc.get("City") or doc.get("state") or doc.get("State"))
                        if region:
                            patient_info["region"] = str(region)
                        
                        # Exercise intensity
                        exercise = (doc.get("exercise_intensity") or doc.get("exerciseIntensity") or 
                                   doc.get("运动强度") or doc.get("exerciseLevel") or doc.get("exercise_level"))
                        if exercise:
                            patient_info["exercise_intensity"] = str(exercise)
                        
                        # Medications
                        medications = (doc.get("medications") or doc.get("Medications") or 
                                      doc.get("current_medications") or doc.get("currentMedications") or
                                      doc.get("用药") or doc.get("meds") or doc.get("Meds"))
                        if medications:
                            patient_info["medications"] = str(medications)
                        
                        # Name fields (for display)
                        name = (doc.get("name") or doc.get("Name") or 
                               doc.get("firstName") or doc.get("first_name") or
                               (doc.get("firstName") and doc.get("lastName") and 
                                f"{doc.get('firstName')} {doc.get('lastName')}"))
                        if name:
                            patient_info["name"] = str(name)
                        
                        # Calculate BMI if weight and height are available but BMI is not
                        if patient_info.get("weight") and patient_info.get("height") and not patient_info.get("bmi"):
                            try:
                                weight_kg = float(patient_info["weight"])
                                height_val = float(patient_info["height"])
                                # Assume height is in cm if > 3, otherwise in meters
                                height_m = height_val / 100.0 if height_val > 3 else height_val
                                if height_m > 0:
                                    patient_info["bmi"] = round(weight_kg / (height_m ** 2), 1)
                            except Exception:
                                pass
                        
                        # If we found any info, return (don't keep searching)
                        if len(patient_info) > 1:  # More than just patient_id
                            print(f"[INFO] Extracted patient info from {collection_name} collection")
                            return patient_info
                except Exception as e:
                    print(f"[DEBUG] Error querying {collection_name} with field {id_field}: {e}")
                    continue
        except Exception as e:
            print(f"[DEBUG] Error accessing collection {collection_name}: {e}")
            continue
    
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
    language: str = 'zh'
) -> str:
    """
    Generate HTML summary similar to the attached image format.
    生成类似附图的HTML总结。
    """
    
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
            'notes': '备注',
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
            'notes': 'Notes',
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
    
    # If patient info summary exists, display it first
    # 如果有现成的patient info summary，优先显示
    if patient_info.get("summary"):
        summary_text = str(patient_info["summary"])
        summary_label = t.get("summary", "患者背景" if language == 'zh' else "Patient Background")
        patient_info_html += f'<li><strong>{summary_label}:</strong> {html.escape(summary_text)}</li>'
    else:
        # Show individual fields if no summary
        if patient_info.get("age"):
            gender = patient_info.get("gender") or ""
            if gender:
                gender_text = t['male'] if gender.lower() in ["male", "m", "男"] else t['female'] if gender.lower() in ["female", "f", "女"] else ""
                gender_text = f", {gender_text}" if gender_text else ""
            else:
                gender_text = ""
            age_suffix = t['years_old'] if language == 'zh' else ' years old'
            age_label = "年龄" if language == 'zh' else "Age"
            patient_info_html += f'<li>{age_label}: {patient_info["age"]}{age_suffix}{gender_text}</li>'
        
        if patient_info.get("weight") and patient_info.get("height"):
            patient_info_html += f'<li>{t["weight"]}: {patient_info["weight"]}kg, {t["height"]}: {patient_info["height"]}cm</li>'
        elif patient_info.get("weight"):
            patient_info_html += f'<li>{t["weight"]}: {patient_info["weight"]}kg</li>'
        elif patient_info.get("height"):
            patient_info_html += f'<li>{t["height"]}: {patient_info["height"]}cm</li>'
        
        if patient_info.get("bmi"):
            patient_info_html += f'<li>BMI: {patient_info["bmi"]}</li>'
        
        if patient_info.get("medical_history"):
            medical_history = str(patient_info["medical_history"])
            patient_info_html += f'<li>{t["medical_history"]}: {html.escape(medical_history)}</li>'
        
        if patient_info.get("ethnicity"):
            patient_info_html += f'<li>{t["ethnicity"]}: {html.escape(str(patient_info["ethnicity"]))}</li>'
        
        if patient_info.get("region"):
            patient_info_html += f'<li>{t["region"]}: {html.escape(str(patient_info["region"]))}</li>'
        
        if patient_info.get("exercise_intensity"):
            exercise = str(patient_info["exercise_intensity"])
            patient_info_html += f'<li>{t["exercise_intensity"]}: {html.escape(exercise)}</li>'
        
        if patient_info.get("medications"):
            medications = str(patient_info["medications"])
            patient_info_html += f'<li>{t["medications"]}: {html.escape(medications)}</li>'
    
    # If no additional info found, show a message
    if len(patient_info) == 1:  # Only patient_id
        no_info_msg = "暂无其他患者信息" if language == 'zh' else "No additional patient information available"
        patient_info_html += f'<li style="color: #999; font-style: italic;">{no_info_msg}</li>'
    
    patient_info_html += """
        </ul>
    </div>
    """
    
    # Title based on language
    if language == 'zh':
        title = "AI食物日志总结 - 赋能患者, 提升照护师效率"
    else:
        title = "AI Food Log Summary - Empowering Patients, Enhancing Care Team Efficiency"
    
    # Food log summary section
    if language == 'zh':
        meal_order = ["早餐", "午餐", "晚餐", "其他"]
    else:
        meal_order = ["Breakfast", "Lunch", "Dinner", "Other"]
    
    food_log_html = '<div class="food-log-summary">'
    food_log_html += f'<h2>{t["food_log_summary"]}</h2>'
    
    # Calculate total calories if possible
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
        
        # Collect all images, ingredients, notes, and comments for this meal
        meal_images = []
        all_ingredients = []
        all_notes = []
        all_comments = []  # Store comments separately
        
        for row in food_logs_by_meal[meal_type]:
            # Collect images - check for ImageURLs first (direct URLs from API)
            image_urls = row.get("ImageURLs")
            if image_urls and isinstance(image_urls, list):
                # Use direct URLs from API - no need to download
                meal_images.extend(image_urls)
            else:
                # Fallback: check ImgName (might be URLs from API or local filenames)
                img_names_str = str(row.get("ImgName", "") or "").strip()
                img_items = [x.strip() for x in img_names_str.split(";") if x.strip()] if img_names_str else []
                
                for img_item in img_items:
                    # Check if it's already a full URL (from API)
                    if img_item.startswith('http://') or img_item.startswith('https://'):
                        # Direct URL from API, use it directly
                        meal_images.append(img_item)
                    else:
                        # Local filename - only process if file exists (backward compatibility)
                        img_path = images_dir / img_item
                        if img_path.exists():
                            if use_data_uri:
                                # Convert to data URI for embedding in HTML
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
                                    if image_base_url:
                                        meal_images.append(f"{image_base_url}/{img_item}")
                                    else:
                                        relative_path = f"{images_dir.name}/{img_item}"
                                        meal_images.append(relative_path)
                            else:
                                # Use URL path
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
            notes_label = "备注" if language == 'zh' else "Notes"
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
                
                # Show comment time if available
                commented_at = comment.get("commentedAt")
                if commented_at:
                    try:
                        # Try to parse and format the date
                        dt = pd.to_datetime(commented_at)
                        if language == 'zh':
                            time_str = dt.strftime('%Y-%m-%d %H:%M')
                        else:
                            time_str = dt.strftime('%Y-%m-%d %I:%M %p')
                        comment_html += f'<div class="comment-time">{time_str}</div>'
                    except:
                        comment_html += f'<div class="comment-time">{html.escape(str(commented_at))}</div>'
                
                comment_html += '</div>'
                food_log_html += comment_html
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
    
    # Parse date
    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
        start_date = target_date.replace(hour=0, minute=0, second=0)
        end_date = target_date.replace(hour=23, minute=59, second=59)
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
