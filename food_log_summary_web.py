#!/usr/bin/env python3
"""
Food Log Summary Web Application
食物日志总结 Web应用

Interactive web application to generate food log summaries with patient ID and date selection.
交互式网页应用，可选择patient ID和日期生成食物日志总结。
"""
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    from flask import Flask, render_template_string, request, jsonify, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("[ERROR] Missing Flask packages. Please install: pip install flask flask-cors", file=sys.stderr)
    sys.exit(1)

try:
    from pymongo import MongoClient
    from bson import ObjectId
    from dotenv import load_dotenv
    import pandas as pd
except ImportError:
    print("[ERROR] Missing required packages. Please install: pip install pymongo pandas python-dotenv", file=sys.stderr)
    sys.exit(1)

# Import functions from existing modules
from query_food_logs import get_mongo_client, query_food_logs
from generate_food_log_summary import (
    get_patient_info,
    get_food_log_image_urls,
    group_food_logs_by_meal,
    generate_html_summary,
    analyze_food_image_with_openai,
    download_image_with_cache,
    generate_weekly_or_monthly_insight
)

try:
    from cache_db import CacheDB
except ImportError:
    CacheDB = None
    print("[WARN] CacheDB not available. Caching will be disabled.", file=sys.stderr)

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for API endpoints

# Configuration
DATABASE_NAME = os.getenv("DATABASE_NAME", "UnifiedCare")
MONGO_URI = os.getenv("MONGO_DATABASE_URI")
SESSION_TOKEN = os.getenv("SESSION_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
IMAGES_DIR = Path("./images")
IMAGES_DIR.mkdir(exist_ok=True, parents=True)


def get_all_patient_ids_with_food_logs(
    client: MongoClient,
    database_name: str = "UnifiedCare"
) -> List[Dict[str, Any]]:
    """
    Get all patient IDs that have food logs, with their food log counts.
    获取所有有食物日志的病人ID，以及他们的食物日志数量。
    
    Returns:
        List of dicts with patient_id and count
    """
    db = client[database_name]
    collection = db["food_logs"]
    
    try:
        # Aggregate to get unique memberIds with counts and latest date
        pipeline = [
            {
                "$match": {
                    "images": {"$exists": True, "$ne": None}
                }
            },
            {
                "$addFields": {
                    "has_images": {
                        "$cond": {
                            "if": {"$isArray": "$images"},
                            "then": {"$gt": [{"$size": "$images"}, 0]},
                            "else": False
                        }
                    }
                }
            },
            {
                "$match": {
                    "has_images": True
                }
            },
            {
                "$group": {
                    "_id": "$memberId",
                    "count": {"$sum": 1},
                    "latest_date": {"$max": "$createdAt"},
                    "earliest_date": {"$min": "$createdAt"},
                    "logs": {"$push": "$createdAt"}  # Keep all log dates for calculating top days
                }
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        
        patient_list = []
        for doc in results:
            member_id = doc["_id"]
            # Convert ObjectId to string if needed
            if isinstance(member_id, ObjectId):
                member_id = str(member_id)
            
            latest_date_raw = doc.get("latest_date")
            earliest_date_raw = doc.get("earliest_date")
            
            # Convert to PT timezone
            try:
                import pytz
                pt_timezone = pytz.timezone('America/Los_Angeles')
            except ImportError:
                print("[WARNING] pytz not installed, using UTC for date conversion", file=sys.stderr)
                pt_timezone = None
            
            def convert_to_pt_datetime(dt):
                """Convert datetime to PT timezone and return datetime object (for sorting)"""
                if not dt:
                    return None
                
                if not pt_timezone:
                    return None
                
                if isinstance(dt, datetime):
                    # If timezone-aware, convert to PT; otherwise assume UTC
                    if dt.tzinfo is not None:
                        return dt.astimezone(pt_timezone)
                    else:
                        # Assume UTC if no timezone info
                        dt_utc = pytz.utc.localize(dt)
                        return dt_utc.astimezone(pt_timezone)
                elif isinstance(dt, str):
                    # Try to parse string and convert to PT
                    try:
                        if dt.endswith('Z'):
                            dt_utc = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                        else:
                            dt_parsed = datetime.fromisoformat(dt)
                            if dt_parsed.tzinfo is not None:
                                dt_utc = dt_parsed
                            else:
                                dt_utc = pytz.utc.localize(dt_parsed)
                        return dt_utc.astimezone(pt_timezone)
                    except:
                        return None
                else:
                    return None
            
            def convert_to_pt_date(dt):
                """Convert datetime to PT timezone and return date string in YYYY-MM-DD format"""
                dt_pt = convert_to_pt_datetime(dt)
                if dt_pt:
                    return dt_pt.date().isoformat()
                return None
            
            # Convert dates to PT timezone for display and sorting
            latest_date = None
            latest_date_pt_datetime = None  # Keep PT datetime for sorting
            if latest_date_raw:
                latest_date_pt_datetime = convert_to_pt_datetime(latest_date_raw)
                latest_date = latest_date_pt_datetime.date().isoformat() if latest_date_pt_datetime else None
            
            earliest_date = None
            if earliest_date_raw:
                earliest_date = convert_to_pt_date(earliest_date_raw)
            
            # Calculate top 5 days with most logs (using PT timezone)
            top_days = []
            logs = doc.get("logs", [])
            if logs:
                # Count logs per day in PT timezone
                from collections import defaultdict
                daily_counts = defaultdict(int)
                for log_date in logs:
                    day_str = convert_to_pt_date(log_date)
                    if day_str:
                        daily_counts[day_str] += 1
                
                # Sort by count (descending) and take top 5
                sorted_days = sorted(daily_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                top_days = [{"date": date_str, "count": count} for date_str, count in sorted_days]
            
            patient_list.append({
                "patient_id": str(member_id),
                "food_log_count": doc.get("count", 0),
                "latest_date": latest_date,  # PT date string for display
                "latest_date_pt_datetime": latest_date_pt_datetime,  # PT datetime for sorting
                "earliest_date": earliest_date,
                "top_days": top_days  # Top 5 days with most logs
            })
        
        # Sort by latest_date_pt_datetime (most recent first, using PT timezone)
        # Patients with no date come last
        def get_sort_key(x):
            dt_pt = x.get("latest_date_pt_datetime")
            if dt_pt:
                return dt_pt
            # Return a very old datetime for patients with no date
            if pt_timezone:
                return pytz.utc.localize(datetime.min).astimezone(pt_timezone)
            return datetime.min
        
        patient_list.sort(key=get_sort_key, reverse=True)
        
        return patient_list
    except Exception as e:
        print(f"[ERROR] Failed to get patient IDs: {e}", file=sys.stderr)
        return []


def get_care_notes(
    client: MongoClient,
    patient_id: str,
    database_name: str = "UnifiedCare"
) -> List[Dict[str, Any]]:
    """
    Get care notes for a patient from uc_care_notes collection.
    从uc_care_notes集合获取病人的care notes。
    
    Args:
        client: MongoDB client / MongoDB客户端
        patient_id: Patient ID / 病人ID
        database_name: Database name / 数据库名称
        
    Returns:
        List of care notes / care notes列表
    """
    db = client[database_name]
    collection = db["uc_care_notes"]
    
    try:
        # Convert patient_id to ObjectId if valid
        # 如果有效，将patient_id转换为ObjectId
        try:
            member_id = ObjectId(patient_id)
        except Exception:
            member_id = patient_id
        
        # Query care notes - try different field names for patient ID
        # 查询care notes - 尝试不同的病人ID字段名
        care_notes = []
        
        # Try memberId field
        # 尝试memberId字段
        query_filter = {"memberId": member_id}
        notes = list(collection.find(query_filter).sort("createdAt", -1))
        if notes:
            care_notes = notes
        else:
            # Try patient_id field
            # 尝试patient_id字段
            query_filter = {"patient_id": member_id}
            notes = list(collection.find(query_filter).sort("createdAt", -1))
            if notes:
                care_notes = notes
            else:
                # Try _id field
                # 尝试_id字段
                query_filter = {"_id": member_id}
                notes = list(collection.find(query_filter).sort("createdAt", -1))
                if notes:
                    care_notes = notes
        
        # Convert to list of dicts and handle ObjectId and datetime
        # 转换为字典列表并处理ObjectId和datetime
        result = []
        for note in care_notes:
            note_dict = {}
            for key, value in note.items():
                if isinstance(value, ObjectId):
                    note_dict[key] = str(value)
                elif isinstance(value, datetime):
                    note_dict[key] = value.isoformat()
                else:
                    note_dict[key] = value
            result.append(note_dict)
        
        return result
    except Exception as e:
        print(f"[WARN] Failed to get care notes for patient {patient_id}: {e}", file=sys.stderr)
        return []


def get_care_note_by_id(
    client: MongoClient,
    patient_id: str,
    note_id: str,
    database_name: str = "UnifiedCare"
) -> Optional[Dict[str, Any]]:
    """
    Get a single care note by ID for a patient.
    根据ID获取单个care note。
    
    Args:
        client: MongoDB client / MongoDB客户端
        patient_id: Patient ID / 病人ID
        note_id: Care note ID / Care note ID
        database_name: Database name / 数据库名称
        
    Returns:
        Care note dict or None / care note字典或None
    """
    db = client[database_name]
    collection = db["uc_care_notes"]
    
    try:
        # Convert IDs to ObjectId if valid
        # 如果有效，将ID转换为ObjectId
        try:
            member_id = ObjectId(patient_id)
        except Exception:
            member_id = patient_id
        
        try:
            note_obj_id = ObjectId(note_id)
        except Exception:
            note_obj_id = note_id
        
        # Try to find the note by _id first
        # 首先尝试通过_id查找note
        note = collection.find_one({"_id": note_obj_id})
        
        if note:
            # Verify it belongs to the patient
            # 验证它属于该病人
            if (str(note.get("memberId", "")) == str(member_id) or 
                str(note.get("patient_id", "")) == str(member_id) or
                str(note.get("_id", "")) == str(member_id)):
                # Convert to dict and handle ObjectId and datetime
                # 转换为字典并处理ObjectId和datetime
                note_dict = {}
                for key, value in note.items():
                    if isinstance(value, ObjectId):
                        note_dict[key] = str(value)
                    elif isinstance(value, datetime):
                        note_dict[key] = value.isoformat()
                    else:
                        note_dict[key] = value
                return note_dict
        
        return None
    except Exception as e:
        print(f"[WARN] Failed to get care note {note_id} for patient {patient_id}: {e}", file=sys.stderr)
        return None


# HTML Template for the web interface
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>食物日志总结生成器</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 20px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, Helvetica, Arial, 'Noto Sans', 'PingFang SC', 'Microsoft Yahei', sans-serif;
            background: #f5f5f5;
            color: #333;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            margin-top: 0;
            color: #2c3e50;
            border-bottom: 3px solid #4a90e2;
            padding-bottom: 15px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #555;
        }
        select, input[type="date"] {
            width: 100%;
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
        }
        select:focus, input[type="date"]:focus {
            outline: none;
            border-color: #4a90e2;
        }
        button {
            background: #4a90e2;
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 6px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.3s;
        }
        button:hover {
            background: #357abd;
        }
        button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        .loading {
            display: none;
            margin-top: 20px;
            text-align: center;
            color: #666;
        }
        .loading.active {
            display: block;
        }
        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #4a90e2;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 10px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .error {
            display: none;
            margin-top: 20px;
            padding: 15px;
            background: #fee;
            border-left: 4px solid #f44;
            border-radius: 4px;
            color: #c33;
        }
        .error.active {
            display: block;
        }
        .info {
            margin-top: 10px;
            padding: 10px;
            background: #e8f4f8;
            border-left: 4px solid #4a90e2;
            border-radius: 4px;
            color: #555;
            font-size: 14px;
        }
        .result-container {
            margin-top: 30px;
            display: none;
        }
        .result-container.active {
            display: block;
        }
        .result-frame {
            width: 100%;
            height: 800px;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
        }
        .language-switch .lang-option.active {
            background: #4a90e2 !important;
            color: white !important;
        }
        .language-switch .lang-option:not(.active) {
            background: white !important;
            color: #4a90e2 !important;
        }
        .patient-info-display {
            margin-top: 10px;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
            font-size: 13px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 id="pageTitle">食物日志总结生成器</h1>
        
        <form id="summaryForm">
            <div class="form-group">
                <label for="patientSelect" id="patientLabel">选择病人 ID:</label>
                <select id="patientSelect" name="patient_id" required>
                    <option value="">加载中...</option>
                </select>
                <div class="patient-info-display" id="patientInfo" style="display: none;">
                    <div id="foodLogCountLabel">食物日志数量: <span id="foodLogCount">-</span></div>
                    <div id="earliestDateLabel">最早日期: <span id="earliestDate">-</span></div>
                    <div id="latestDateLabel">最晚日期: <span id="latestDate">-</span></div>
                    <div id="topDaysLabel" style="margin-top: 10px; font-weight: 600;">日志数量最多的五天:</div>
                    <div id="topDaysList" style="margin-left: 10px; margin-top: 5px;"></div>
                </div>
                <div class="patient-info-display" id="careNotesInfo" style="display: none; margin-top: 15px;">
                    <div id="careNotesLabel" style="font-weight: 600; margin-bottom: 8px;">Care Notes:</div>
                    <div id="careNotesList" style="margin-left: 10px;"></div>
                </div>
            </div>
            
            <div class="form-group">
                <label for="dateSelect" id="dateLabel">选择日期:</label>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <input type="date" id="dateSelect" name="date" required style="flex: 1; max-width: 200px;"/>
                    <div class="language-switch" id="languageSwitch" style="display: flex; border: 2px solid #4a90e2; border-radius: 6px; overflow: hidden; cursor: pointer; user-select: none;">
                        <span class="lang-option" data-lang="zh" style="padding: 6px 12px; background: #4a90e2; color: white; font-size: 13px; font-weight: 600;">中</span>
                        <span class="lang-option" data-lang="en" style="padding: 6px 12px; background: white; color: #4a90e2; font-size: 13px; font-weight: 600;">EN</span>
                    </div>
                </div>
            </div>
            
            <div class="form-group">
                <label>
                    <input type="checkbox" id="debugCheckbox" name="debug"/>
                    <span id="debugLabel">Debug模式（显示数据结构）</span>
                </label>
            </div>
            
            <div style="margin-top: 20px; margin-bottom: 15px;">
                <h3 id="nutritionInsightsTitle" style="margin: 0; color: #2c3e50; font-size: 20px; font-weight: 600;">生成营养洞察 / Generate Nutrition Insights</h3>
            </div>
            <div style="display: flex; gap: 15px; align-items: flex-start; flex-wrap: wrap;">
                <button type="submit" id="generateBtn">Daily Summary</button>
                <button type="button" id="generateWeeklyBtn" style="background: #4a90e2;">
                    <span id="generateWeeklyText">Weekly Insights</span>
                </button>
                <button type="button" id="generateMonthlyBtn" style="background: #4a90e2;">
                    <span id="generateMonthlyText">Monthly Insights</span>
                </button>
            </div>
        </form>
        
        <div class="loading" id="periodLoading" style="margin-top: 20px;">
            <div class="spinner"></div>
            <div id="periodLoadingText">正在生成洞察，请稍候...</div>
            <div id="periodProgress" style="margin-top: 10px; font-size: 14px; color: #666;"></div>
        </div>
        
        <div class="error" id="periodError"></div>
        
        <div class="result-container" id="periodResultContainer" style="margin-top: 20px;">
            <h2 id="periodResultTitle" style="color: #2c3e50; font-size: 24px; margin-bottom: 15px;">生成的洞察</h2>
            <div id="periodResultText" style="padding: 25px; background: linear-gradient(135deg, #f8f9fa 0%, #ffffff 100%); border-radius: 12px; border-left: 5px solid #4a90e2; box-shadow: 0 2px 8px rgba(0,0,0,0.1); color: #555; font-size: 15px; line-height: 1.8;"></div>
        </div>
        
        <div style="margin-top: 30px; padding-top: 30px; border-top: 2px solid #e0e0e0;">
            <h3 id="batchCacheTitle" style="margin-top: 0; color: #2c3e50;">批量生成缓存 / Batch Cache Generation</h3>
            <p id="batchCacheDesc" style="color: #666; margin-bottom: 20px;">为多个病人批量生成图片和AI摘要缓存，提高后续查询速度</p>
            
            <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                <button type="button" id="generateCacheCurrentBtn" style="background: #28a745;">
                    <span id="generateCurrentCacheText">生成当前病人缓存</span><br/>
                    <small id="cacheMonthNote" style="font-size: 12px; opacity: 0.9;">(近一个月)</small>
                </button>
                <button type="button" id="generateCacheActiveBtn" style="background: #17a2b8;">
                    <span id="generateActiveCacheText">生成活跃病人缓存</span><br/>
                    <small id="cacheActiveNote" style="font-size: 12px; opacity: 0.9;">(每日log数>1，近一个月)</small>
                </button>
            </div>
            
            <div class="loading" id="cacheLoading" style="margin-top: 20px;">
                <div class="spinner"></div>
                <div id="cacheLoadingText">正在生成缓存，请稍候...</div>
                <div id="cacheProgress" style="margin-top: 10px; font-size: 14px; color: #666;"></div>
            </div>
            
            <div class="error" id="cacheError"></div>
        </div>
        
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <div id="loadingText">正在生成食物日志总结，请稍候...</div>
        </div>
        
        <div class="error" id="error"></div>
        
        <div class="result-container" id="resultContainer">
            <h2 id="resultTitle">生成的总结</h2>
            <iframe id="resultFrame" class="result-frame" src=""></iframe>
        </div>
    </div>
    
    <script>
        // Language translations
        const translations = {
            zh: {
                title: '食物日志总结生成器',
                selectPatient: '选择病人 ID:',
                selectDate: '选择日期:',
                generate: '生成总结',
                loading: '正在生成食物日志总结，请稍候...',
                resultTitle: '生成的总结',
                foodLogCount: '食物日志数量:',
                earliestDate: '最早日期:',
                latestDate: '最晚日期:',
                topDays: '日志数量最多的五天:',
                careNotes: 'Care Notes:',
                noCareNotes: '暂无Care Notes',
                pleaseSelect: '请选择病人 ID...',
                loadingText: '加载中...',
                errorLoading: '加载失败，请刷新页面重试',
                errorSelect: '请选择病人 ID 和日期',
                records: '条记录',
                debugMode: 'Debug模式（显示数据结构）',
                batchCacheTitle: '批量生成缓存 / Batch Cache Generation',
                batchCacheDesc: '为多个病人批量生成图片和AI摘要缓存，提高后续查询速度',
                generateCurrentCache: '生成当前病人缓存',
                generateActiveCache: '生成活跃病人缓存',
                cacheMonthNote: '(近一个月)',
                cacheActiveNote: '(总log数>100 且 周log数>5，近一个月)',
                cacheLoading: '正在生成缓存，请稍候...',
                cacheProgress: '正在生成缓存...',
                cacheComplete: '完成！生成了 {images} 张图片缓存和 {summaries} 个AI摘要缓存。',
                cacheActiveComplete: '完成！处理了 {patients} 个活跃病人，生成了 {images} 张图片缓存和 {summaries} 个AI摘要缓存。',
                cacheError: '生成缓存失败',
                selectPatientFirst: '请先选择一个病人',
                confirmCurrentCache: '确定要为当前病人（{id}）生成近一个月的缓存吗？',
                confirmActiveCache: '将获取符合条件的活跃病人列表（总log数>100 且 周log数>5），然后逐个确认并处理。是否继续？',
                gettingActiveList: '正在获取活跃病人列表...',
                foundActivePatients: '找到 {count} 个活跃病人。将逐个确认并处理。是否继续？',
                noActivePatients: '未找到符合条件的活跃病人',
                processingPatient: '正在处理病人 {id} ({current}/{total})...',
                completedPatient: '已完成 {current}/{total}: {id} ({images} 图片, {summaries} 摘要)',
                skippedPatient: '跳过 {id} ({current}/{total})',
                finalSummary: '完成！处理了 {processed}/{total} 个病人，生成了 {images} 张图片缓存和 {summaries} 个AI摘要缓存。',
                generateWeekly: 'Weekly Insights',
                generateMonthly: 'Monthly Insights',
                periodLoading: '正在生成洞察，请稍候...',
                periodError: '生成洞察失败',
                periodResultTitle: '生成的洞察',
                selectPatientForPeriod: '请先选择病人ID',
                nutritionInsightsTitle: '生成营养洞察 / Generate Nutrition Insights',
                dailySummary: 'Daily Summary'
            },
            en: {
                title: 'Food Log Summary Generator',
                selectPatient: 'Select Patient ID:',
                selectDate: 'Select Date:',
                generate: 'Generate Summary',
                loading: 'Generating food log summary, please wait...',
                resultTitle: 'Generated Summary',
                foodLogCount: 'Food Log Count:',
                earliestDate: 'Earliest Date:',
                latestDate: 'Latest Date:',
                topDays: 'Top 5 Days with Most Logs:',
                careNotes: 'Care Notes:',
                noCareNotes: 'No Care Notes',
                pleaseSelect: 'Please select Patient ID...',
                loadingText: 'Loading...',
                errorLoading: 'Failed to load, please refresh and try again',
                errorSelect: 'Please select Patient ID and Date',
                records: ' records',
                debugMode: 'Debug Mode (Show Raw API Data)',
                batchCacheTitle: 'Batch Cache Generation',
                batchCacheDesc: 'Batch generate image and AI summary cache for multiple patients to improve query speed',
                generateCurrentCache: 'Generate Current Patient Cache',
                generateActiveCache: 'Generate Active Patients Cache',
                cacheMonthNote: '(Last 30 days)',
                cacheActiveNote: '(Total logs > 100 AND weekly logs > 5, last 30 days)',
                cacheLoading: 'Generating cache, please wait...',
                cacheProgress: 'Generating cache...',
                cacheComplete: 'Complete! Generated {images} image caches and {summaries} AI summary caches.',
                cacheActiveComplete: 'Complete! Processed {patients} active patients, generated {images} image caches and {summaries} AI summary caches.',
                cacheError: 'Failed to generate cache',
                selectPatientFirst: 'Please select a patient first',
                confirmCurrentCache: 'Are you sure you want to generate cache for the current patient ({id}) for the last 30 days?',
                confirmActiveCache: 'Will get list of active patients (total logs > 100 AND weekly logs > 5), then process one by one with confirmation. Continue?',
                gettingActiveList: 'Getting active patients list...',
                foundActivePatients: 'Found {count} active patients. Will process one by one with confirmation. Continue?',
                noActivePatients: 'No active patients found matching criteria',
                processingPatient: 'Processing patient {id} ({current}/{total})...',
                completedPatient: 'Completed {current}/{total}: {id} ({images} images, {summaries} summaries)',
                skippedPatient: 'Skipped {id} ({current}/{total})',
                finalSummary: 'Complete! Processed {processed}/{total} patients, generated {images} image caches and {summaries} AI summary caches.',
                generateWeekly: 'Weekly Insights',
                generateMonthly: 'Monthly Insights',
                periodLoading: 'Generating insights, please wait...',
                periodError: 'Failed to generate insights',
                periodResultTitle: 'Generated Insights',
                selectPatientForPeriod: 'Please select Patient ID first',
                nutritionInsightsTitle: 'Generate Nutrition Insights',
                dailySummary: 'Daily Summary'
            }
        };
        
        let currentLang = localStorage.getItem('language') || 'zh';
        
        // Update UI language
        function updateLanguage(lang) {
            currentLang = lang;
            localStorage.setItem('language', lang);
            const t = translations[lang];
            
            document.getElementById('pageTitle').textContent = t.title;
            document.getElementById('patientLabel').textContent = t.selectPatient;
            document.getElementById('dateLabel').textContent = t.selectDate;
            document.getElementById('debugLabel').textContent = t.debugMode;
            document.getElementById('generateBtn').textContent = t.dailySummary;
            
            // Update nutrition insights title
            const nutritionInsightsTitle = document.getElementById('nutritionInsightsTitle');
            if (nutritionInsightsTitle) nutritionInsightsTitle.textContent = t.nutritionInsightsTitle;
            document.getElementById('loadingText').textContent = t.loading;
            document.getElementById('resultTitle').textContent = t.resultTitle;
            
            // Update cache generation section
            // 更新缓存生成部分
            const batchCacheTitle = document.getElementById('batchCacheTitle');
            const batchCacheDesc = document.getElementById('batchCacheDesc');
            const generateCurrentCacheText = document.getElementById('generateCurrentCacheText');
            const generateActiveCacheText = document.getElementById('generateActiveCacheText');
            const cacheMonthNote = document.getElementById('cacheMonthNote');
            const cacheActiveNote = document.getElementById('cacheActiveNote');
            const cacheLoadingText = document.getElementById('cacheLoadingText');
            
            if (batchCacheTitle) batchCacheTitle.textContent = t.batchCacheTitle;
            if (batchCacheDesc) batchCacheDesc.textContent = t.batchCacheDesc;
            if (generateCurrentCacheText) generateCurrentCacheText.textContent = t.generateCurrentCache;
            if (generateActiveCacheText) generateActiveCacheText.textContent = t.generateActiveCache;
            if (cacheMonthNote) cacheMonthNote.textContent = t.cacheMonthNote;
            if (cacheActiveNote) cacheActiveNote.textContent = t.cacheActiveNote;
            if (cacheLoadingText) cacheLoadingText.textContent = t.cacheLoading;
            
            // Update period insights section
            const generateWeeklyText = document.getElementById('generateWeeklyText');
            const generateMonthlyText = document.getElementById('generateMonthlyText');
            const periodLoadingText = document.getElementById('periodLoadingText');
            const periodResultTitle = document.getElementById('periodResultTitle');
            
            if (generateWeeklyText) generateWeeklyText.textContent = t.generateWeekly;
            if (generateMonthlyText) generateMonthlyText.textContent = t.generateMonthly;
            if (periodLoadingText) periodLoadingText.textContent = t.periodLoading;
            if (periodResultTitle) periodResultTitle.textContent = t.periodResultTitle;
            
            // Update labels for patient info section
            const patientInfo = document.getElementById('patientInfo');
            if (patientInfo) {
                // Get current values if they exist
                const foodLogCountEl = document.getElementById('foodLogCount');
                const earliestDateEl = document.getElementById('earliestDate');
                const latestDateEl = document.getElementById('latestDate');
                const count = foodLogCountEl && foodLogCountEl.textContent !== '-' ? foodLogCountEl.textContent : '-';
                const earliest = earliestDateEl && earliestDateEl.textContent !== '-' ? earliestDateEl.textContent : '-';
                const latest = latestDateEl && latestDateEl.textContent !== '-' ? latestDateEl.textContent : '-';
                
                // Update labels with preserved or default values
                document.getElementById('foodLogCountLabel').innerHTML = `${t.foodLogCount} <span id="foodLogCount">${count}</span>`;
                document.getElementById('earliestDateLabel').innerHTML = `${t.earliestDate} <span id="earliestDate">${earliest}</span>`;
                document.getElementById('latestDateLabel').innerHTML = `${t.latestDate} <span id="latestDate">${latest}</span>`;
                
                // Update top days label if element exists
                const topDaysLabelEl = document.getElementById('topDaysLabel');
                if (topDaysLabelEl) {
                    topDaysLabelEl.textContent = t.topDays;
                }
                
                // Update care notes label if element exists
                const careNotesLabelEl = document.getElementById('careNotesLabel');
                if (careNotesLabelEl) {
                    careNotesLabelEl.textContent = t.careNotes;
                }
            }
        }
        
        // Language switch click handler
        document.querySelectorAll('.lang-option').forEach(option => {
            option.addEventListener('click', function() {
                const lang = this.dataset.lang;
                if (lang !== currentLang) {
                    // Update active state
                    document.querySelectorAll('.lang-option').forEach(opt => {
                        opt.classList.remove('active');
                    });
                    this.classList.add('active');
                    
                    updateLanguage(lang);
                    // Reload patient IDs to update text
                    loadPatientIDs();
                    
                    // Reload patient info if selected
                    const patientId = document.getElementById('patientSelect').value;
                    if (patientId) {
                        const option = document.getElementById('patientSelect').options[document.getElementById('patientSelect').selectedIndex];
                        if (option.value) {
                            const t = translations[currentLang];
                            document.getElementById('foodLogCount').textContent = option.dataset.count;
                            const locale = currentLang === 'zh' ? 'zh-CN' : 'en-US';
                            
                            // Format dates in PT timezone (dates are already in PT from backend)
                            const formatPTDate = (dateStr) => {
                                if (!dateStr) return '-';
                                try {
                                    const [year, month, day] = dateStr.split('-');
                                    const dateObj = new Date(year, parseInt(month) - 1, day);
                                    return dateObj.toLocaleDateString(locale, { timeZone: 'America/Los_Angeles' });
                                } catch (e) {
                                    return dateStr;
                                }
                            };
                            
                            document.getElementById('earliestDate').textContent = formatPTDate(option.dataset.earliest);
                            document.getElementById('latestDate').textContent = formatPTDate(option.dataset.latest);
                            
                            // Update top 5 days
                            const topDaysList = document.getElementById('topDaysList');
                            try {
                                const topDays = JSON.parse(option.dataset.topDays || '[]');
                                if (topDays && topDays.length > 0) {
                                    let html = '';
                                    topDays.forEach(day => {
                                        // Date is already in PT timezone from backend (YYYY-MM-DD format)
                                        // Format it directly without timezone conversion to preserve PT date
                                        let dateStr = day.date;
                                        try {
                                            const [year, month, dayNum] = day.date.split('-');
                                            // Format as MM/DD/YYYY (US format) or YYYY-MM-DD (other locales)
                                            // Since date is already in PT, just format the string directly
                                            if (locale === 'zh-CN' || locale.startsWith('zh')) {
                                                dateStr = `${year}-${month}-${dayNum}`;
                                            } else {
                                                dateStr = `${month}/${dayNum}/${year}`;
                                            }
                                        } catch (e) {
                                            // Keep original date string if parsing fails
                                        }
                                        html += `<div style="margin: 3px 0;">${dateStr} - ${day.count} ${t.records}</div>`;
                                    });
                                    topDaysList.innerHTML = html;
                                } else {
                                    const noDataText = currentLang === 'zh' ? '暂无数据' : 'No data';
                                    topDaysList.innerHTML = `<div style="color: #999;">${noDataText}</div>`;
                                }
                            } catch (e) {
                                const noDataText = currentLang === 'zh' ? '暂无数据' : 'No data';
                                topDaysList.innerHTML = `<div style="color: #999;">${noDataText}</div>`;
                            }
                        }
                    }
                }
            });
        });
        
        // Initialize language - clear all active states first, then set the correct one
        document.querySelectorAll('.lang-option').forEach(opt => {
            opt.classList.remove('active');
        });
        const initialLangOption = document.querySelector(`.lang-option[data-lang="${currentLang}"]`);
        if (initialLangOption) {
            initialLangOption.classList.add('active');
        } else {
            // Fallback to Chinese if currentLang doesn't match any option
            document.querySelector('.lang-option[data-lang="zh"]').classList.add('active');
            currentLang = 'zh';
        }
        updateLanguage(currentLang);
        
        // Set today as default date in PT timezone
        function getTodayPT() {
            const now = new Date();
            // Convert to PT timezone
            const ptDate = new Date(now.toLocaleString('en-US', { timeZone: 'America/Los_Angeles' }));
            // Format as YYYY-MM-DD for date input
            const year = ptDate.getFullYear();
            const month = String(ptDate.getMonth() + 1).padStart(2, '0');
            const day = String(ptDate.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        }
        document.getElementById('dateSelect').value = getTodayPT();
        
        // Load patient IDs
        // Store the change handler function reference to allow removal
        // 存储 change 处理函数引用以便移除
        let patientSelectChangeHandler = null;
        
        function loadPatientIDs() {
            const t = translations[currentLang];
            const select = document.getElementById('patientSelect');
            
            // Remove existing change event listener if any
            // 如果存在，先移除旧的 change 事件监听器
            if (patientSelectChangeHandler) {
                select.removeEventListener('change', patientSelectChangeHandler);
                patientSelectChangeHandler = null;
            }
            
            fetch('/api/patient-ids')
                .then(response => response.json())
                .then(data => {
                    select.innerHTML = `<option value="">${t.pleaseSelect}</option>`;
                    
                    if (data.error) {
                        select.innerHTML = `<option value="">${t.errorLoading}: ${data.error}</option>`;
                        return;
                    }
                    
                    data.patients.forEach(patient => {
                        const option = document.createElement('option');
                        option.value = patient.patient_id;
                        option.textContent = `${patient.patient_id} (${patient.food_log_count} ${t.records})`;
                        option.dataset.count = patient.food_log_count;
                        option.dataset.earliest = patient.earliest_date || '';
                        option.dataset.latest = patient.latest_date || '';
                        option.dataset.topDays = JSON.stringify(patient.top_days || []);
                        select.appendChild(option);
                    });
                    
                    // Update patient info when selection changes
                    // 创建命名函数以便后续可以移除
                    patientSelectChangeHandler = function() {
                        const option = this.options[this.selectedIndex];
                        if (option && option.value) {
                            const t = translations[currentLang];
                            const locale = currentLang === 'zh' ? 'zh-CN' : 'en-US';
                            
                            // Get values first
                            const count = option.dataset.count || '-';
                            // Dates are already in PT timezone from backend (YYYY-MM-DD format), format for display
                            const formatPTDate = (dateStr) => {
                                if (!dateStr) return '-';
                                try {
                                    // Parse as PT date (YYYY-MM-DD format from backend)
                                    const [year, month, day] = dateStr.split('-');
                                    const dateObj = new Date(year, parseInt(month) - 1, day);
                                    return dateObj.toLocaleDateString(locale, { timeZone: 'America/Los_Angeles' });
                                } catch (e) {
                                    return dateStr;
                                }
                            };
                            const earliestDateStr = formatPTDate(option.dataset.earliest);
                            const latestDateStr = formatPTDate(option.dataset.latest);
                            
                            // Update values in spans
                            document.getElementById('foodLogCount').textContent = count;
                            document.getElementById('earliestDate').textContent = earliestDateStr;
                            document.getElementById('latestDate').textContent = latestDateStr;
                            
                            // Update labels with values embedded
                            document.getElementById('foodLogCountLabel').innerHTML = `${t.foodLogCount} <span id="foodLogCount">${count}</span>`;
                            document.getElementById('earliestDateLabel').innerHTML = `${t.earliestDate} <span id="earliestDate">${earliestDateStr}</span>`;
                            document.getElementById('latestDateLabel').innerHTML = `${t.latestDate} <span id="latestDate">${latestDateStr}</span>`;
                            document.getElementById('topDaysLabel').textContent = t.topDays;
                            
                            // Display top 5 days
                            const topDaysList = document.getElementById('topDaysList');
                            try {
                                const topDays = JSON.parse(option.dataset.topDays || '[]');
                                if (topDays && topDays.length > 0) {
                                    let html = '';
                                    topDays.forEach(day => {
                                        // Date is already in PT timezone from backend (YYYY-MM-DD format)
                                        // Format it directly without timezone conversion to preserve PT date
                                        let dateStr = day.date;
                                        try {
                                            const [year, month, dayNum] = day.date.split('-');
                                            // Format as MM/DD/YYYY (US format) or YYYY-MM-DD (other locales)
                                            // Since date is already in PT, just format the string directly
                                            if (locale === 'zh-CN' || locale.startsWith('zh')) {
                                                dateStr = `${year}-${month}-${dayNum}`;
                                            } else {
                                                dateStr = `${month}/${dayNum}/${year}`;
                                            }
                                        } catch (e) {
                                            // Keep original date string if parsing fails
                                        }
                                        html += `<div style="margin: 3px 0;">${dateStr} - ${day.count} ${t.records}</div>`;
                                    });
                                    topDaysList.innerHTML = html;
                                } else {
                                    const noDataText = currentLang === 'zh' ? '暂无数据' : 'No data';
                                    topDaysList.innerHTML = `<div style="color: #999;">${noDataText}</div>`;
                                }
                            } catch (e) {
                                const noDataText = currentLang === 'zh' ? '暂无数据' : 'No data';
                                topDaysList.innerHTML = `<div style="color: #999;">${noDataText}</div>`;
                            }
                            
                            document.getElementById('patientInfo').style.display = 'block';
                        } else {
                            document.getElementById('patientInfo').style.display = 'none';
                            document.getElementById('careNotesInfo').style.display = 'none';
                        }
                    };
                    
                    // Add the event listener
                    // 添加事件监听器
                    select.addEventListener('change', patientSelectChangeHandler);
                })
                .catch(error => {
                    console.error('Error loading patient IDs:', error);
                    const t = translations[currentLang];
                    document.getElementById('patientSelect').innerHTML = 
                        `<option value="">${t.errorLoading}</option>`;
                });
        }
        
        loadPatientIDs();
        
        // Handle batch cache generation buttons
        // 处理批量生成缓存按钮
        document.getElementById('generateCacheCurrentBtn').addEventListener('click', async function() {
            const t = translations[currentLang];
            const patientId = document.getElementById('patientSelect').value;
            if (!patientId) {
                alert(t.selectPatientFirst);
                return;
            }
            
            const confirmMsg = t.confirmCurrentCache.replace('{id}', patientId);
            if (!confirm(confirmMsg)) {
                return;
            }
            
            const cacheLoading = document.getElementById('cacheLoading');
            const cacheError = document.getElementById('cacheError');
            const cacheProgress = document.getElementById('cacheProgress');
            const btn = document.getElementById('generateCacheCurrentBtn');
            
            cacheLoading.classList.add('active');
            cacheError.classList.remove('active');
            cacheError.textContent = '';
            btn.disabled = true;
            cacheProgress.textContent = t.cacheProgress;
            
            try {
                const response = await fetch('/api/generate-cache-current-patient', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({patient_id: patientId})
                });
                
                const data = await response.json();
                
                if (data.error) {
                    cacheError.textContent = data.error;
                    cacheError.classList.add('active');
                } else {
                    const completeMsg = t.cacheComplete
                        .replace('{images}', data.total_images)
                        .replace('{summaries}', data.total_summaries);
                    cacheProgress.textContent = completeMsg;
                }
            } catch (error) {
                cacheError.textContent = t.cacheError + ': ' + error.message;
                cacheError.classList.add('active');
            } finally {
                cacheLoading.classList.remove('active');
                btn.disabled = false;
            }
        });
        
        document.getElementById('generateCacheActiveBtn').addEventListener('click', async function() {
            const t = translations[currentLang];
            const cacheLoading = document.getElementById('cacheLoading');
            const cacheError = document.getElementById('cacheError');
            const cacheProgress = document.getElementById('cacheProgress');
            const btn = document.getElementById('generateCacheActiveBtn');
            
            cacheLoading.classList.add('active');
            cacheError.classList.remove('active');
            cacheError.textContent = '';
            btn.disabled = true;
            
            try {
                // Get patient list from the select element
                // 从选择元素获取病人列表
                const select = document.getElementById('patientSelect');
                const patients = [];
                for (let i = 0; i < select.options.length; i++) {
                    const option = select.options[i];
                    if (option.value) {
                        patients.push({
                            patient_id: option.value,
                            food_log_count: parseInt(option.dataset.count) || 0
                        });
                    }
                }
                
                if (patients.length === 0) {
                    cacheProgress.textContent = currentLang === 'zh' ? '没有可用的病人' : 'No patients available';
                    return;
                }
                
                const confirmText = currentLang === 'zh'
                    ? `将遍历 ${patients.length} 个病人，符合条件的将逐个确认并处理。是否继续？`
                    : `Will iterate through ${patients.length} patients, eligible ones will be processed one by one with confirmation. Continue?`;
                
                if (!confirm(confirmText)) {
                    return;
                }
                
                // Process each patient one by one
                // 逐个处理每个病人
                let totalImages = 0;
                let totalSummaries = 0;
                let processedCount = 0;
                let skippedCount = 0;
                let cachedCount = 0;
                
                for (let i = 0; i < patients.length; i++) {
                    const patient = patients[i];
                    
                    // Update progress
                    const checkingText = currentLang === 'zh'
                        ? `正在检查病人 ${patient.patient_id} (${i + 1}/${patients.length})...`
                        : `Checking patient ${patient.patient_id} (${i + 1}/${patients.length})...`;
                    cacheProgress.textContent = checkingText;
                    
                    try {
                        // Check criteria and generate cache
                        const response = await fetch('/api/generate-cache-for-patient', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                patient_id: patient.patient_id,
                                check_criteria: true  // Check if patient meets criteria
                            })
                        });
                        
                        const data = await response.json();
                        
                        if (data.error) {
                            console.error(`Failed to process patient ${patient.patient_id}:`, data.error);
                            continue;
                        }
                        
                        // Check if patient was skipped (doesn't meet criteria)
                        if (data.skipped) {
                            skippedCount++;
                            continue;
                        }
                        
                        // Check if already fully cached
                        if (data.is_fully_cached) {
                            cachedCount++;
                            const cachedText = currentLang === 'zh'
                                ? `病人 ${patient.patient_id} 已完全缓存，跳过 (${i + 1}/${patients.length})`
                                : `Patient ${patient.patient_id} is fully cached, skipped (${i + 1}/${patients.length})`;
                            cacheProgress.textContent = cachedText;
                            continue;
                        }
                        
                        // Patient meets criteria and needs processing
                        const patientInfo = currentLang === 'zh'
                            ? `病人 ${patient.patient_id}\n总log数: ${patient.food_log_count}`
                            : `Patient ${patient.patient_id}\nTotal logs: ${patient.food_log_count}`;
                        
                        if (data.cached_images > 0 || data.cached_summaries > 0) {
                            const cachedInfo = currentLang === 'zh'
                                ? `\n已有缓存: ${data.cached_images} 图片, ${data.cached_summaries} 摘要`
                                : `\nExisting cache: ${data.cached_images} images, ${data.cached_summaries} summaries`;
                            patientInfo += cachedInfo;
                        }
                        
                        const confirmMsg = currentLang === 'zh'
                            ? `${patientInfo}\n\n是否为此病人生成缓存？\n(${i + 1}/${patients.length})`
                            : `${patientInfo}\n\nGenerate cache for this patient?\n(${i + 1}/${patients.length})`;
                        
                        if (!confirm(confirmMsg)) {
                            skippedCount++;
                            continue;
                        }
                        
                        // Update progress
                        const processingText = t.processingPatient
                            .replace('{id}', patient.patient_id)
                            .replace('{current}', i + 1)
                            .replace('{total}', patients.length);
                        cacheProgress.textContent = processingText;
                        
                        // Process this patient (without criteria check since we already checked)
                        const processResponse = await fetch('/api/generate-cache-for-patient', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                patient_id: patient.patient_id,
                                check_criteria: false  // Already checked, just process
                            })
                        });
                        
                        const processData = await processResponse.json();
                        
                        if (processData.error) {
                            console.error(`Failed to process patient ${patient.patient_id}:`, processData.error);
                            continue;
                        }
                        
                        totalImages += processData.total_images || 0;
                        totalSummaries += processData.total_summaries || 0;
                        processedCount++;
                        
                        const progressText = t.completedPatient
                            .replace('{current}', i + 1)
                            .replace('{total}', patients.length)
                            .replace('{id}', patient.patient_id)
                            .replace('{images}', processData.total_images || 0)
                            .replace('{summaries}', processData.total_summaries || 0);
                        cacheProgress.textContent = progressText;
                        
                    } catch (error) {
                        console.error(`Error processing patient ${patient.patient_id}:`, error);
                        continue;
                    }
                }
                
                // Final summary
                const finalMsg = currentLang === 'zh'
                    ? `完成！处理了 ${processedCount} 个病人，跳过了 ${skippedCount} 个，${cachedCount} 个已完全缓存。生成了 ${totalImages} 张图片缓存和 ${totalSummaries} 个AI摘要缓存。`
                    : `Complete! Processed ${processedCount} patients, skipped ${skippedCount}, ${cachedCount} fully cached. Generated ${totalImages} image caches and ${totalSummaries} AI summary caches.`;
                cacheProgress.textContent = finalMsg;
                
            } catch (error) {
                cacheError.textContent = t.cacheError + ': ' + error.message;
                cacheError.classList.add('active');
            } finally {
                cacheLoading.classList.remove('active');
                btn.disabled = false;
            }
        });
        
        // Handle form submission
        document.getElementById('summaryForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const patientId = document.getElementById('patientSelect').value;
            const date = document.getElementById('dateSelect').value;
            const t = translations[currentLang];
            
            if (!patientId || !date) {
                showError(t.errorSelect);
                return;
            }
            
            // Show loading
            document.getElementById('loading').classList.add('active');
            document.getElementById('error').classList.remove('active');
            document.getElementById('resultContainer').classList.remove('active');
            document.getElementById('generateBtn').disabled = true;
            // Hide care notes during loading
            document.getElementById('careNotesInfo').style.display = 'none';
            
            try {
                const debugMode = document.getElementById('debugCheckbox').checked;
                const response = await fetch('/api/generate-summary', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                    patient_id: patientId,
                    date: date,
                    language: currentLang,
                    debug: debugMode
                    })
                });
                
                const data = await response.json();
                
                if (data.error) {
                    showError(data.error);
                } else {
                    // Show result in iframe
                    const frame = document.getElementById('resultFrame');
                    frame.srcdoc = data.html;
                    document.getElementById('resultContainer').classList.add('active');
                    
                        // Show debug info if available
                    if (data.debug) {
                        console.log('=== Debug Information ===');
                        console.log('Patient Info:', data.debug.patient_info);
                        console.log('Food Logs Count:', data.debug.food_logs_count);
                        console.log('Food Logs Raw Data (from MongoDB):', data.debug.food_logs_raw);
                        console.log('API Responses (from GET /food-log/{id}):', data.debug.api_responses);
                        
                        // Always show care notes info, even if empty
                        // 始终显示 care notes 信息，即使为空
                        console.log('\\n=== Care Notes (from uc_care_notes) ===');
                        if (data.debug.care_notes !== undefined) {
                            if (data.debug.care_notes && data.debug.care_notes.length > 0) {
                                console.log(`Found ${data.debug.care_notes.length} care note(s):`);
                                data.debug.care_notes.forEach((note, index) => {
                                    console.log(`\\nCare Note ${index + 1}:`, JSON.stringify(note, null, 2));
                                });
                            } else {
                                console.log('Care Notes: [] (No care notes found for this patient)');
                            }
                        } else {
                            console.log('Care Notes: undefined (not queried)');
                        }
                        console.log('========================');
                        
                        // Show complete data in console
                        if (data.debug.food_logs_raw && data.debug.food_logs_raw.length > 0) {
                            console.log('\\n=== Complete Food Logs Raw Data ===');
                            data.debug.food_logs_raw.forEach((foodLog, index) => {
                                console.log(`\\nFood Log ${index + 1}:`, JSON.stringify(foodLog, null, 2));
                            });
                        }
                        
                        if (data.debug.api_responses && Object.keys(data.debug.api_responses).length > 0) {
                            console.log('\\n=== Complete API Responses ===');
                            for (const [foodLogId, response] of Object.entries(data.debug.api_responses)) {
                                console.log(`\\nFoodLog ID: ${foodLogId}`, JSON.stringify(response, null, 2));
                            }
                        }
                        
                        // Show summary in alert
                        let debugText = '=== Debug Information ===\\n\\n';
                        debugText += 'Food Logs Count: ' + data.debug.food_logs_count + '\\n';
                        if (data.debug.care_notes !== undefined) {
                            debugText += 'Care Notes Count: ' + (data.debug.care_notes ? data.debug.care_notes.length : 0) + '\\n';
                        }
                        debugText += '\\nComplete data available in browser console (F12)\\n';
                        debugText += '\\n- Food Logs Raw Data (from MongoDB)\\n';
                        debugText += '- API Responses (from GET /food-log/{id})\\n';
                        if (data.debug.care_notes !== undefined) {
                            debugText += '- Care Notes (from uc_care_notes)\\n';
                        }
                        debugText += '\\nOpen browser console to see full details.';
                        
                        alert(debugText);
                    }
                    
                    // Care notes are now displayed in the generated HTML summary (Patient Information section)
                    // Care notes 现在显示在生成的 HTML 总结中（Patient Information 部分）
                    // Hide care notes section on the main page
                    // 在主页面隐藏 care notes 部分
                    const careNotesInfo = document.getElementById('careNotesInfo');
                    if (careNotesInfo) {
                        careNotesInfo.style.display = 'none';
                    }
                    
                    // Scroll to result
                    frame.scrollIntoView({ behavior: 'smooth' });
                }
            } catch (error) {
                const t = translations[currentLang];
                showError((currentLang === 'zh' ? '生成失败: ' : 'Generation failed: ') + error.message);
            } finally {
                document.getElementById('loading').classList.remove('active');
                document.getElementById('generateBtn').disabled = false;
            }
        });
        
        // Handle weekly insight button
        document.getElementById('generateWeeklyBtn').addEventListener('click', async function() {
            const t = translations[currentLang];
            const patientId = document.getElementById('patientSelect').value;
            
            if (!patientId) {
                alert(t.selectPatientForPeriod);
                return;
            }
            
            const periodLoading = document.getElementById('periodLoading');
            const periodError = document.getElementById('periodError');
            const periodResultContainer = document.getElementById('periodResultContainer');
            const periodResultText = document.getElementById('periodResultText');
            const btn = document.getElementById('generateWeeklyBtn');
            
            periodLoading.classList.add('active');
            periodError.classList.remove('active');
            periodResultContainer.classList.remove('active');
            periodError.textContent = '';
            btn.disabled = true;
            
            try {
                const periodProgress = document.getElementById('periodProgress');
                if (periodProgress) {
                    periodProgress.textContent = t.periodLoading;
                }
                
                const response = await fetch('/api/generate-weekly-insight', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        patient_id: patientId,
                        language: currentLang
                    })
                });
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                
                if (data.error) {
                    periodError.textContent = data.error;
                    periodError.classList.add('active');
                } else if (data.insight) {
                    periodResultText.textContent = data.insight;
                    periodResultContainer.classList.add('active');
                    if (periodProgress) {
                        periodProgress.textContent = '';
                    }
                } else {
                    throw new Error('No insight data received');
                }
            } catch (error) {
                console.error('Error generating weekly insight:', error);
                periodError.textContent = t.periodError + ': ' + error.message;
                periodError.classList.add('active');
            } finally {
                periodLoading.classList.remove('active');
                btn.disabled = false;
            }
        });
        
        // Handle monthly insight button
        document.getElementById('generateMonthlyBtn').addEventListener('click', async function() {
            const t = translations[currentLang];
            const patientId = document.getElementById('patientSelect').value;
            
            if (!patientId) {
                alert(t.selectPatientForPeriod);
                return;
            }
            
            const periodLoading = document.getElementById('periodLoading');
            const periodError = document.getElementById('periodError');
            const periodResultContainer = document.getElementById('periodResultContainer');
            const periodResultText = document.getElementById('periodResultText');
            const btn = document.getElementById('generateMonthlyBtn');
            
            periodLoading.classList.add('active');
            periodError.classList.remove('active');
            periodResultContainer.classList.remove('active');
            periodError.textContent = '';
            btn.disabled = true;
            
            try {
                const periodProgress = document.getElementById('periodProgress');
                if (periodProgress) {
                    periodProgress.textContent = t.periodLoading;
                }
                
                const response = await fetch('/api/generate-monthly-insight', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        patient_id: patientId,
                        language: currentLang
                    })
                });
                
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                
                const data = await response.json();
                
                if (data.error) {
                    periodError.textContent = data.error;
                    periodError.classList.add('active');
                } else if (data.insight) {
                    periodResultText.textContent = data.insight;
                    periodResultContainer.classList.add('active');
                    if (periodProgress) {
                        periodProgress.textContent = '';
                    }
                } else {
                    throw new Error('No insight data received');
                }
            } catch (error) {
                console.error('Error generating monthly insight:', error);
                periodError.textContent = t.periodError + ': ' + error.message;
                periodError.classList.add('active');
            } finally {
                periodLoading.classList.remove('active');
                btn.disabled = false;
            }
        });
        
        function showError(message) {
            const errorDiv = document.getElementById('error');
            errorDiv.textContent = message;
            errorDiv.classList.add('active');
        }
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """Main page with patient ID and date selector."""
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/patient-ids')
def api_patient_ids():
    """API endpoint to get all patient IDs with food logs."""
    try:
        client = get_mongo_client(MONGO_URI)
        patients = get_all_patient_ids_with_food_logs(client, DATABASE_NAME)
        client.close()
        
        return jsonify({
            "success": True,
            "patients": patients
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/generate-summary', methods=['POST'])
def api_generate_summary():
    """API endpoint to generate food log summary."""
    try:
        data = request.json
        patient_id = data.get('patient_id')
        date_str = data.get('date')
        language = data.get('language', 'zh')  # Default to Chinese
        debug = data.get('debug', False)
        
        if not patient_id or not date_str:
            return jsonify({"error": "Missing patient_id or date"}), 400
        
        # Parse date (assuming input date is in PT timezone)
        # Convert to UTC for MongoDB query since MongoDB stores dates in UTC
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            # Assume the input date is in PT timezone
            try:
                import pytz
                pt_timezone = pytz.timezone('America/Los_Angeles')
                # Create start and end of day in PT timezone
                start_date_pt = pt_timezone.localize(target_date.replace(hour=0, minute=0, second=0, microsecond=0))
                end_date_pt = pt_timezone.localize(target_date.replace(hour=23, minute=59, second=59, microsecond=999999))
                # Convert to UTC for MongoDB query
                start_date = start_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
                end_date = end_date_pt.astimezone(pytz.utc).replace(tzinfo=None)
            except ImportError:
                # Fallback if pytz not available
                start_date = target_date.replace(hour=0, minute=0, second=0)
                end_date = target_date.replace(hour=23, minute=59, second=59)
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
        
        # Connect to MongoDB
        client = get_mongo_client(MONGO_URI)
        
        # Initialize cache database
        # 初始化缓存数据库
        cache_db = None
        if CacheDB:
            cache_db = CacheDB(db_path=Path("./cache.db"))
            if debug:
                stats = cache_db.get_cache_stats()
                print(f"[DEBUG] Cache stats: {stats['image_cache_count']} images, {stats['ai_summary_cache_count']} summaries")
        
        try:
            # Get patient info
            patient_info = get_patient_info(client, patient_id, DATABASE_NAME)
            
            # Get care notes
            care_notes = get_care_notes(client, patient_id, DATABASE_NAME)
            if debug:
                print(f"[DEBUG] Care notes count: {len(care_notes) if care_notes else 0}")
                if care_notes:
                    print(f"[DEBUG] Sample care note: {care_notes[0] if len(care_notes) > 0 else 'N/A'}")
            
            # Query food logs
            food_logs_df = query_food_logs(
                client,
                [patient_id],
                database_name=DATABASE_NAME,
                start_date=start_date,
                end_date=end_date
            )
            
            # Setup images directory
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            
            # Handle empty food logs - still show patient info and care notes
            # 处理空的 food logs - 仍然显示病人信息和 care notes
            has_food_logs = not food_logs_df.empty
            
            # Download images if session token is provided (only if there are food logs)
            # Note: This will also try to extract images from MongoDB images field
            api_responses = {}
            if has_food_logs:
                if debug:
                    print(f"[DEBUG] Before image download - ImgName column exists: {'ImgName' in food_logs_df.columns}")
                    if 'images' in food_logs_df.columns:
                        print(f"[DEBUG] Sample images field: {food_logs_df.iloc[0]['images'] if not food_logs_df.empty else 'N/A'}")
                
                # Get image URLs from API
                if SESSION_TOKEN:
                    if debug:
                        print("[DEBUG] Debug mode: ON - will show detailed FoodLog ID information")
                    food_logs_df, api_responses = get_food_log_image_urls(
                        food_logs_df,
                        SESSION_TOKEN,
                        debug=debug
                    )
                
                if debug:
                    print(f"[DEBUG] After image download - ImgName column: {food_logs_df['ImgName'].tolist() if 'ImgName' in food_logs_df.columns else 'Not found'}")
                
                # Generate AI meal summaries for images using OpenAI
                # 使用 OpenAI 为图片生成 AI meal summaries
                meal_summaries = {}  # Dict mapping image_url -> summary
                if OPENAI_API_KEY and has_food_logs:
                    print("[INFO] Generating AI meal summaries for food log images... / 正在为食物日志图片生成AI meal summaries...")
                    for _, row in food_logs_df.iterrows():
                        # Get food log ID
                        # 获取 food log ID
                        food_log_id = None
                        for id_col in ["_id", "FoodLogId", "foodLogId"]:
                            if id_col in row and pd.notna(row[id_col]):
                                food_log_id = str(row[id_col]).strip()
                                break
                        
                        # Get image URLs
                        image_urls = row.get("ImageURLs")
                        if not image_urls or not isinstance(image_urls, list):
                            # Fallback to ImgName
                            img_names_str = str(row.get("ImgName", "") or "").strip()
                            if img_names_str:
                                image_urls = [url.strip() for url in img_names_str.split(";") if url.strip() and (url.startswith('http://') or url.startswith('https://'))]
                        
                        # Extract patient notes from this food log entry (Description field)
                        # 从这条 food log 记录中提取病人备注（Description 字段）
                        patient_notes_text = None
                        if 'Description' in row and pd.notna(row['Description']):
                            patient_notes_text = str(row['Description']).strip()
                        elif 'note' in row and pd.notna(row['note']):
                            patient_notes_text = str(row['note']).strip()
                        elif 'description' in row and pd.notna(row['description']):
                            patient_notes_text = str(row['description']).strip()
                        
                        if image_urls:
                            # Extract date from food log
                            # 从 food log 中提取日期
                            food_log_date = date_str  # Use the summary date as default
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
                            
                            # Analyze first image (or all images if needed)
                            # 分析第一张图片（或根据需要分析所有图片）
                            for img_url in image_urls[:1]:  # Analyze first image only for now
                                if img_url not in meal_summaries:  # Avoid duplicate analysis
                                    if debug:
                                        print(f"[DEBUG] Analyzing image for food_log_id: {food_log_id}")
                                        if patient_notes_text:
                                            print(f"[DEBUG] Patient notes for this image: {patient_notes_text[:100]}...")
                                    summary = analyze_food_image_with_openai(
                                        img_url,
                                        openai_api_key=OPENAI_API_KEY,
                                        patient_notes=patient_notes_text,
                                        food_log_id=food_log_id,
                                        patient_id=patient_id,
                                        date=food_log_date,
                                        cache_db=cache_db,
                                        debug=debug
                                    )
                                    if summary:
                                        meal_summaries[img_url] = summary
                                        if debug:
                                            print(f"[DEBUG] Generated summary for image: {summary.get('ai_title', 'N/A')}")
                    print(f"[INFO] Generated {len(meal_summaries)} AI meal summaries / 生成了 {len(meal_summaries)} 个AI meal summaries")
                elif not OPENAI_API_KEY and has_food_logs:
                    print("[WARN] OPENAI_API_KEY not found in .env, skipping AI meal summary generation / 未找到OPENAI_API_KEY，跳过AI meal summary生成")
                
                # Group by meal type
                food_logs_by_meal = group_food_logs_by_meal(food_logs_df, language=language)
            else:
                # No food logs - create empty meal structure
                # 没有 food logs - 创建空的 meal 结构
                food_logs_by_meal = {
                    "breakfast": [],
                    "lunch": [],
                    "dinner": [],
                    "snack": [],
                    "other": []
                }
                meal_summaries = {}  # Empty meal summaries when no food logs
            
            # Prepare debug info if requested
            debug_info = {}
            if debug:
                # Convert DataFrame to list of dicts for JSON serialization
                food_logs_raw = []
                if has_food_logs:
                    for _, row in food_logs_df.iterrows():
                        food_log_dict = {}
                        for col in food_logs_df.columns:
                            val = row[col]
                            # Handle different data types safely
                            try:
                                if pd.isna(val):
                                    food_log_dict[col] = None
                                elif isinstance(val, (list, dict)):
                                    # Keep as is for JSON serialization
                                    food_log_dict[col] = val
                                elif isinstance(val, (pd.Timestamp, datetime)):
                                    # Convert datetime to ISO string
                                    food_log_dict[col] = val.isoformat() if hasattr(val, 'isoformat') else str(val)
                                elif isinstance(val, ObjectId):
                                    # Convert ObjectId to string
                                    food_log_dict[col] = str(val)
                                else:
                                    food_log_dict[col] = val
                            except (TypeError, ValueError) as e:
                                # If conversion fails, try to stringify
                                try:
                                    food_log_dict[col] = str(val)
                                except:
                                    food_log_dict[col] = f"<unable to convert: {type(val)}>"
                        food_logs_raw.append(food_log_dict)
                
                debug_info = {
                    "patient_info": patient_info,
                    "food_logs_count": len(food_logs_df) if has_food_logs else 0,
                    "food_logs_raw": food_logs_raw,  # Complete raw food log data from MongoDB
                    "api_responses": api_responses,  # Complete API responses for each food log
                    "care_notes": care_notes,  # Care notes from uc_care_notes collection
                    "has_food_logs": has_food_logs  # Flag indicating if food logs exist
                }
            
            # Generate HTML (use data URI for images in iframe)
            html_content = generate_html_summary(
                patient_info,
                food_logs_by_meal,
                IMAGES_DIR,
                date_str,
                patient_id,
                use_data_uri=True,  # Use data URI for iframe compatibility
                image_base_url=None,
                language=language,
                care_notes=care_notes,  # Pass care notes to include in Patient Information section
                meal_summaries=meal_summaries,  # Pass meal summaries for display
                cache_db=cache_db  # Pass cache database for image caching
            )
            
            client.close()
            
            response = {
                "success": True,
                "html": html_content,
                "care_notes": care_notes
            }
            
            if debug:
                response["debug"] = debug_info
            
            return jsonify(response)
            
        finally:
            client.close()
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/images/<path:filename>')
def serve_image(filename):
    """Serve images from images directory."""
    return send_from_directory(str(IMAGES_DIR), filename)


def _generate_cache_for_patient_internal(patient_id: str, check_criteria: bool = False):
    """
    Internal function to generate cache for a patient in the last 30 days.
    Shared by both current patient and active patients endpoints.
    
    Args:
        patient_id: Patient ID
        check_criteria: If True, check if patient meets criteria (total logs > 100 AND weekly logs > 5)
    """
    from datetime import timedelta
    from query_food_logs import query_patient_all_logs
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    num_days = 30
    weeks_in_month = num_days / 7  # Approximately 4.3 weeks
    
    client = get_mongo_client(MONGO_URI)
    cache_db = CacheDB(db_path=Path("./cache.db")) if CacheDB else None
    
    # Check criteria if requested
    if check_criteria:
        # Get total food logs (all time)
        all_logs_info = query_patient_all_logs(client, patient_id, DATABASE_NAME)
        total_logs = all_logs_info.get("total_logs", 0)
        
        if total_logs <= 100:
            client.close()
            return {
                "success": False,
                "skipped": True,
                "reason": "total_logs_too_low",
                "total_logs": total_logs,
                "message": f"Patient {patient_id} does not meet criteria: total logs ({total_logs}) <= 100"
            }
        
        # Query food logs for this patient in the last month
        food_logs_df_check = query_food_logs(
            client,
            [patient_id],
            database_name=DATABASE_NAME,
            start_date=start_date,
            end_date=end_date
        )
        
        if food_logs_df_check.empty:
            client.close()
            return {
                "success": False,
                "skipped": True,
                "reason": "no_logs_in_month",
                "message": f"Patient {patient_id} has no food logs in the last 30 days"
            }
        
        # Calculate weekly average in last month
        logs_in_month = len(food_logs_df_check)
        weekly_logs = logs_in_month / weeks_in_month if weeks_in_month > 0 else 0
        
        if weekly_logs <= 5:
            client.close()
            return {
                "success": False,
                "skipped": True,
                "reason": "weekly_logs_too_low",
                "total_logs": total_logs,
                "logs_in_month": logs_in_month,
                "weekly_logs": round(weekly_logs, 2),
                "message": f"Patient {patient_id} does not meet criteria: weekly logs ({weekly_logs:.2f}) <= 5"
            }
    
    total_images = 0
    total_summaries = 0
    cached_images = 0
    cached_summaries = 0
    
    print(f"[INFO] Starting cache generation for patient {patient_id} (last 30 days)")
    
    # Query food logs for this patient
    food_logs_df = query_food_logs(
        client,
        [patient_id],
        database_name=DATABASE_NAME,
        start_date=start_date,
        end_date=end_date
    )
    
    if food_logs_df.empty:
        client.close()
        return {
            "success": True,
            "total_images": 0,
            "total_summaries": 0,
            "cached_images": 0,
            "cached_summaries": 0,
            "is_fully_cached": False,
            "message": "No food logs found for this patient in the last 30 days"
        }
    
    # Get image URLs
    if SESSION_TOKEN:
        food_logs_df, _ = get_food_log_image_urls(
            food_logs_df,
            SESSION_TOKEN,
            debug=False
        )
    
    # Collect food log IDs for cache status check
    food_log_ids = []
    for _, row in food_logs_df.iterrows():
        food_log_id = None
        for id_col in ["_id", "FoodLogId", "foodLogId"]:
            if id_col in row and pd.notna(row[id_col]):
                food_log_id = str(row[id_col]).strip()
                break
        if food_log_id:
            food_log_ids.append(food_log_id)
    
    # Check cache status before processing
    cache_status = None
    if cache_db and food_log_ids:
        cache_status = cache_db.check_patient_cache_status(food_log_ids)
        cached_images = cache_status.get("cached_images", 0)
        cached_summaries = cache_status.get("cached_summaries", 0)
    
    # Process each food log
    for _, row in food_logs_df.iterrows():
        # Get food log ID
        food_log_id = None
        for id_col in ["_id", "FoodLogId", "foodLogId"]:
            if id_col in row and pd.notna(row[id_col]):
                food_log_id = str(row[id_col]).strip()
                break
        
        if not food_log_id:
            continue
        
        # Extract date from food log (from createdAt or Date field)
        # 从 food log 中提取日期（从 createdAt 或 Date 字段）
        food_log_date = None
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
        
        # If no date found, use today's date as fallback
        # 如果找不到日期，使用今天的日期作为回退
        if not food_log_date:
            food_log_date = datetime.now().strftime('%Y-%m-%d')
        
        # Get image URLs
        image_urls = row.get("ImageURLs")
        if not image_urls or not isinstance(image_urls, list):
            img_names_str = str(row.get("ImgName", "") or "").strip()
            if img_names_str:
                image_urls = [url.strip() for url in img_names_str.split(";") 
                            if url.strip() and (url.startswith('http://') or url.startswith('https://'))]
        
        # Download images and generate AI summaries
        patient_notes_text = None
        if 'Description' in row and pd.notna(row['Description']):
            patient_notes_text = str(row['Description']).strip()
        
        if image_urls:
            for image_index, img_url in enumerate(image_urls):
                # Download image with cache
                local_path = download_image_with_cache(
                    img_url,
                    IMAGES_DIR,
                    food_log_id=food_log_id,
                    image_index=image_index,
                    patient_id=patient_id,
                    date=food_log_date,
                    cache_db=cache_db,
                    debug=False
                )
                if local_path:
                    total_images += 1
                
                # Generate AI summary with cache
                if OPENAI_API_KEY:
                    summary = analyze_food_image_with_openai(
                        img_url,
                        openai_api_key=OPENAI_API_KEY,
                        patient_notes=patient_notes_text,
                        food_log_id=food_log_id,
                        patient_id=patient_id,
                        date=food_log_date,
                        cache_db=cache_db,
                        debug=False
                    )
                    if summary:
                        total_summaries += 1
    
    client.close()
    
    # Determine if fully cached (all food logs have both images and summaries cached)
    is_fully_cached = False
    if cache_status and food_log_ids:
        # Rough check: if we have cached entries for all food logs
        total_expected = len(food_log_ids)  # At least one image per food log
        is_fully_cached = (cached_images >= total_expected and cached_summaries >= total_expected)
    
    return {
        "success": True,
        "total_images": total_images,
        "total_summaries": total_summaries,
        "cached_images": cached_images,
        "cached_summaries": cached_summaries,
        "is_fully_cached": is_fully_cached
    }


@app.route('/api/generate-cache-current-patient', methods=['POST'])
def api_generate_cache_current_patient():
    """API endpoint to generate cache for current selected patient in the last month."""
    try:
        data = request.json
        patient_id = data.get('patient_id')
        
        if not patient_id:
            return jsonify({
                "success": False,
                "error": "Patient ID is required"
            }), 400
        
        result = _generate_cache_for_patient_internal(patient_id)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/generate-cache-for-patient', methods=['POST'])
def api_generate_cache_for_patient():
    """API endpoint to generate cache for a single patient (used by active patients batch processing)."""
    try:
        data = request.json
        patient_id = data.get('patient_id')
        check_criteria = data.get('check_criteria', False)  # Check if patient meets criteria
        
        if not patient_id:
            return jsonify({
                "success": False,
                "error": "Patient ID is required"
            }), 400
        
        # Use the same logic as current patient cache generation
        result = _generate_cache_for_patient_internal(patient_id, check_criteria=check_criteria)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/generate-weekly-insight', methods=['POST'])
def api_generate_weekly_insight():
    """API endpoint to generate weekly nutrition insight."""
    try:
        data = request.json
        patient_id = data.get('patient_id')
        language = data.get('language', 'zh')
        
        if not patient_id:
            return jsonify({"error": "Patient ID is required"}), 400
        
        if not OPENAI_API_KEY:
            return jsonify({"error": "OPENAI_API_KEY not configured"}), 500
        
        # Get patient info
        client = get_mongo_client(MONGO_URI)
        patient_info = get_patient_info(client, patient_id)
        client.close()
        
        # Calculate date range (past 7 days)
        import pytz
        end_date = datetime.now(pytz.timezone("America/Los_Angeles"))
        start_date = end_date - timedelta(days=7)
        
        # Generate insight
        cache_db = CacheDB(db_path=Path("./cache.db")) if CacheDB else None
        insight = generate_weekly_or_monthly_insight(
            patient_info,
            patient_id,
            start_date,
            end_date,
            'weekly',
            openai_api_key=OPENAI_API_KEY,
            language=language,
            cache_db=cache_db,
            session_token=SESSION_TOKEN,
            debug=False
        )
        
        if insight:
            return jsonify({"insight": insight})
        else:
            return jsonify({"error": "Failed to generate weekly insight"}), 500
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate-monthly-insight', methods=['POST'])
def api_generate_monthly_insight():
    """API endpoint to generate monthly nutrition insight."""
    try:
        data = request.json
        patient_id = data.get('patient_id')
        language = data.get('language', 'zh')
        
        if not patient_id:
            return jsonify({"error": "Patient ID is required"}), 400
        
        if not OPENAI_API_KEY:
            return jsonify({"error": "OPENAI_API_KEY not configured"}), 500
        
        # Get patient info
        client = get_mongo_client(MONGO_URI)
        patient_info = get_patient_info(client, patient_id)
        client.close()
        
        # Calculate date range (past 30 days)
        import pytz
        end_date = datetime.now(pytz.timezone("America/Los_Angeles"))
        start_date = end_date - timedelta(days=30)
        
        # Generate insight
        cache_db = CacheDB(db_path=Path("./cache.db")) if CacheDB else None
        insight = generate_weekly_or_monthly_insight(
            patient_info,
            patient_id,
            start_date,
            end_date,
            'monthly',
            openai_api_key=OPENAI_API_KEY,
            language=language,
            cache_db=cache_db,
            session_token=SESSION_TOKEN,
            debug=False
        )
        
        if insight:
            return jsonify({"insight": insight})
        else:
            return jsonify({"error": "Failed to generate monthly insight"}), 500
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/care-note/<patient_id>/<note_id>')
def care_note_detail(patient_id, note_id):
    """Display detailed care note page."""
    try:
        client = get_mongo_client(MONGO_URI)
        
        # Try to get note by ID first
        # 首先尝试通过ID获取note
        note = get_care_note_by_id(client, patient_id, note_id, DATABASE_NAME)
        
        # If not found and note_id is a number, try to get by index
        # 如果未找到且note_id是数字，尝试通过索引获取
        if not note and note_id.isdigit():
            all_notes = get_care_notes(client, patient_id, DATABASE_NAME)
            try:
                index = int(note_id)
                if 0 <= index < len(all_notes):
                    note = all_notes[index]
            except (ValueError, IndexError):
                pass
        
        client.close()
        
        if not note:
            return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>Care Note Not Found</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; text-align: center; }
                    .error { color: #d32f2f; }
                </style>
            </head>
            <body>
                <h1 class="error">Care Note Not Found</h1>
                <p>The requested care note could not be found.</p>
                <a href="/">← Back to Home</a>
            </body>
            </html>
            """), 404
        
        # Format note content
        note_content = ''
        if note.get('note'):
            note_content = note['note']
        elif note.get('content'):
            note_content = note['content']
        elif note.get('text'):
            note_content = note['text']
        else:
            # Show all fields
            fields = []
            for key, value in note.items():
                if key not in ['_id', 'memberId', 'patient_id']:
                    if isinstance(value, dict):
                        fields.append(f"<strong>{key}:</strong> {json.dumps(value, indent=2, ensure_ascii=False)}")
                    elif isinstance(value, list):
                        fields.append(f"<strong>{key}:</strong> {json.dumps(value, indent=2, ensure_ascii=False)}")
                    else:
                        fields.append(f"<strong>{key}:</strong> {value}")
            note_content = '<br>'.join(fields) if fields else 'No content'
        
        # Format date
        date_str = ''
        if note.get('createdAt'):
            try:
                date = datetime.fromisoformat(note['createdAt'].replace('Z', '+00:00'))
                date_str = date.strftime('%Y-%m-%d %H:%M:%S')
            except:
                date_str = str(note.get('createdAt', ''))
        
        # Create HTML page
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Care Note Details</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, Helvetica, Arial, sans-serif;
                    margin: 0;
                    padding: 20px;
                    background: #f5f5f5;
                    color: #333;
                }}
                .container {{
                    max-width: 900px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 8px;
                    padding: 30px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                h1 {{
                    margin-top: 0;
                    color: #2c3e50;
                    border-bottom: 3px solid #4a90e2;
                    padding-bottom: 15px;
                }}
                .meta {{
                    color: #666;
                    font-size: 14px;
                    margin-bottom: 20px;
                    padding-bottom: 15px;
                    border-bottom: 1px solid #e0e0e0;
                }}
                .content {{
                    font-size: 15px;
                    line-height: 1.6;
                    white-space: pre-wrap;
                    word-wrap: break-word;
                }}
                .back-link {{
                    display: inline-block;
                    margin-top: 20px;
                    color: #4a90e2;
                    text-decoration: none;
                    font-weight: 600;
                }}
                .back-link:hover {{
                    text-decoration: underline;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Care Note Details</h1>
                <div class="meta">
                    <strong>Patient ID:</strong> {patient_id}<br>
                    {f'<strong>Date:</strong> {date_str}<br>' if date_str else ''}
                    <strong>Note ID:</strong> {note_id}
                </div>
                <div class="content">{note_content}</div>
                <a href="/" class="back-link">← Back to Home</a>
            </div>
        </body>
        </html>
        """
        
        return render_template_string(html)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template_string(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Error</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                .error {{ color: #d32f2f; }}
            </style>
        </head>
        <body>
            <h1 class="error">Error</h1>
            <p>{str(e)}</p>
            <a href="/">← Back to Home</a>
        </body>
        </html>
        """), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

