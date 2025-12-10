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


def get_patient_info(client: MongoClient, patient_id: str, database_name: str = "UnifiedCare") -> Dict[str, Any]:
    """
    Get patient information from database.
    从数据库获取患者信息。
    
    Args:
        client: MongoDB client
        patient_id: Patient ID
        database_name: Database name
        
    Returns:
        Dict with patient information
    """
    db = client[database_name]
    patient_info = {}
    
    # Try to get from patients or members collection
    # 尝试从patients或members集合获取
    for collection_name in ["patients", "members", "uc_enrolled_programs"]:
        try:
            collection = db[collection_name]
            
            # Try different ID fields
            for id_field in ["_id", "patient_id", "memberId", "member_id"]:
                try:
                    # Convert patient_id to ObjectId if possible
                    query_id = patient_id
                    if id_field == "_id" or id_field == "memberId":
                        try:
                            query_id = ObjectId(patient_id)
                        except Exception:
                            pass
                    
                    query = {id_field: query_id}
                    doc = collection.find_one(query)
                    
                    if doc:
                        # Extract patient information
                        patient_info.update({
                            "age": doc.get("age") or doc.get("Age") or None,
                            "gender": doc.get("gender") or doc.get("Gender") or doc.get("sex") or None,
                            "weight": doc.get("weight") or doc.get("Weight") or None,
                            "height": doc.get("height") or doc.get("Height") or None,
                            "bmi": doc.get("bmi") or doc.get("BMI") or None,
                            "medical_history": doc.get("medical_history") or doc.get("medicalHistory") or doc.get("disease_history") or None,
                            "ethnicity": doc.get("ethnicity") or doc.get("Ethnicity") or doc.get("民族") or None,
                            "region": doc.get("region") or doc.get("Region") or doc.get("地域") or doc.get("location") or None,
                            "exercise_intensity": doc.get("exercise_intensity") or doc.get("exerciseIntensity") or doc.get("运动强度") or None,
                            "medications": doc.get("medications") or doc.get("Medications") or doc.get("current_medications") or doc.get("用药") or None,
                        })
                        
                        # Calculate BMI if weight and height are available
                        if patient_info.get("weight") and patient_info.get("height") and not patient_info.get("bmi"):
                            try:
                                weight_kg = float(patient_info["weight"])
                                height_m = float(patient_info["height"]) / 100.0  # Convert cm to m
                                if height_m > 0:
                                    patient_info["bmi"] = round(weight_kg / (height_m ** 2), 1)
                            except Exception:
                                pass
                        
                        if patient_info:
                            print(f"[INFO] Found patient info from {collection_name} collection")
                            return patient_info
                except Exception as e:
                    continue
        except Exception as e:
            continue
    
    print(f"[WARN] Could not find patient info for {patient_id}, using defaults")
    return patient_info


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


def parse_meal_type(row: pd.Series) -> str:
    """
    Parse meal type from food log row.
    从食物日志行解析餐次类型。
    
    Returns: "早餐", "午餐", "晚餐", or "其他"
    """
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
                return "早餐"
            elif 10 <= hour < 14:
                return "午餐"
            elif 14 <= hour < 20:
                return "晚餐"
            else:
                return "其他"
        
        return "其他"
    
    meal_lower = meal_title.lower()
    
    # English
    if any(keyword in meal_lower for keyword in ["breakfast", "morning"]):
        return "早餐"
    if any(keyword in meal_lower for keyword in ["lunch", "noon", "midday"]):
        return "午餐"
    if any(keyword in meal_lower for keyword in ["dinner", "evening", "night", "supper"]):
        return "晚餐"
    if any(keyword in meal_lower for keyword in ["snack", "snacks"]):
        return "其他"
    
    # Chinese
    if "早餐" in meal_title or "早饭" in meal_title or "早" in meal_title:
        return "早餐"
    if "午餐" in meal_title or "午饭" in meal_title or "午" in meal_title:
        return "午餐"
    if "晚餐" in meal_title or "晚饭" in meal_title or "晚" in meal_title or "夜" in meal_title:
        return "晚餐"
    
    return "其他"


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


def group_food_logs_by_meal(food_logs_df: pd.DataFrame) -> Dict[str, List[pd.Series]]:
    """
    Group food logs by meal type.
    按餐次类型分组食物日志。
    
    Returns:
        Dict with keys: "早餐", "午餐", "晚餐", "其他"
    """
    grouped = defaultdict(list)
    
    for _, row in food_logs_df.iterrows():
        meal_type = parse_meal_type(row)
        grouped[meal_type].append(row)
    
    return dict(grouped)


def generate_html_summary(
    patient_info: Dict[str, Any],
    food_logs_by_meal: Dict[str, List[pd.Series]],
    images_dir: Path,
    date: str,
    patient_id: str,
    use_data_uri: bool = True,
    image_base_url: Optional[str] = None
) -> str:
    """
    Generate HTML summary similar to the attached image format.
    生成类似附图的HTML总结。
    """
    
    # Patient info section
    patient_info_html = """
    <div class="patient-info">
        <h2>患者信息</h2>
        <ul class="patient-details">
    """
    
    if patient_info.get("age"):
        gender = patient_info.get("gender") or ""
        if gender:
            gender_text = "男性" if gender.lower() in ["male", "m", "男"] else "女性" if gender.lower() in ["female", "f", "女"] else ""
        else:
            gender_text = ""
        patient_info_html += f'<li>{patient_info["age"]}岁{gender_text}</li>'
    
    if patient_info.get("weight") and patient_info.get("height"):
        patient_info_html += f'<li>{patient_info["weight"]}kg {patient_info["height"]}cm</li>'
    elif patient_info.get("weight"):
        patient_info_html += f'<li>体重: {patient_info["weight"]}kg</li>'
    elif patient_info.get("height"):
        patient_info_html += f'<li>身高: {patient_info["height"]}cm</li>'
    
    if patient_info.get("bmi"):
        patient_info_html += f'<li>BMI: {patient_info["bmi"]}</li>'
    
    if patient_info.get("medical_history"):
        medical_history = str(patient_info["medical_history"])
        patient_info_html += f'<li>疾病史: {html.escape(medical_history)}</li>'
    
    if patient_info.get("ethnicity"):
        patient_info_html += f'<li>民族: {html.escape(str(patient_info["ethnicity"]))}</li>'
    
    if patient_info.get("region"):
        patient_info_html += f'<li>地域: {html.escape(str(patient_info["region"]))}</li>'
    
    if patient_info.get("exercise_intensity"):
        exercise = str(patient_info["exercise_intensity"])
        patient_info_html += f'<li>运动强度: {html.escape(exercise)}</li>'
    
    if patient_info.get("medications"):
        medications = str(patient_info["medications"])
        patient_info_html += f'<li>当前用药: {html.escape(medications)}</li>'
    
    patient_info_html += """
        </ul>
    </div>
    """
    
    # Food log summary section
    meal_order = ["早餐", "午餐", "晚餐", "其他"]
    food_log_html = '<div class="food-log-summary">'
    food_log_html += f'<h2>AI 1日食物日志总结</h2>'
    
    # Calculate total calories if possible
    total_calories_info = ""
    
    for meal_type in meal_order:
        if meal_type not in food_logs_by_meal or not food_logs_by_meal[meal_type]:
            continue
        
        food_log_html += f'<div class="meal-section">'
        food_log_html += f'<h3>{meal_type}</h3>'
        
        # Collect all images and ingredients for this meal
        meal_images = []
        all_ingredients = []
        all_notes = []
        
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
            
            # Collect notes
            description = str(row.get("Description", "") or row.get("description", "") or "").strip()
            if description:
                all_notes.append(description)
            
            rd_comments = row.get("RD Comments") or row.get("rd_comments")
            if rd_comments:
                rd_text = str(rd_comments).strip()
                if rd_text:
                    all_notes.append(rd_text)
        
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
            food_log_html += '<div class="ingredients-label">主要食材：</div>'
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
        
        # Display notes
        if all_notes:
            food_log_html += '<div class="meal-notes">'
            for note in all_notes:
                food_log_html += f'<p>{html.escape(note).replace(chr(10), "<br/>")}</p>'
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
  background: white;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.8;
  color: #555;
  border-left: 3px solid #4a90e2;
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
    <h1>AI食谱 - 赋能患者,提升照护师效率</h1>
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
