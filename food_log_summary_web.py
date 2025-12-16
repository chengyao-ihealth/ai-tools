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
from datetime import datetime
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
    generate_html_summary
)

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS for API endpoints

# Configuration
DATABASE_NAME = os.getenv("DATABASE_NAME", "UnifiedCare")
MONGO_URI = os.getenv("MONGO_DATABASE_URI")
SESSION_TOKEN = os.getenv("SESSION_TOKEN")
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
            
            <button type="submit" id="generateBtn">生成总结</button>
        </form>
        
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
                pleaseSelect: '请选择病人 ID...',
                loadingText: '加载中...',
                errorLoading: '加载失败，请刷新页面重试',
                errorSelect: '请选择病人 ID 和日期',
                records: '条记录',
                debugMode: 'Debug模式（显示数据结构）'
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
                pleaseSelect: 'Please select Patient ID...',
                loadingText: 'Loading...',
                errorLoading: 'Failed to load, please refresh and try again',
                errorSelect: 'Please select Patient ID and Date',
                records: ' records',
                debugMode: 'Debug Mode (Show Raw API Data)'
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
            document.getElementById('generateBtn').textContent = t.generate;
            document.getElementById('loadingText').textContent = t.loading;
            document.getElementById('resultTitle').textContent = t.resultTitle;
            
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
        function loadPatientIDs() {
            const t = translations[currentLang];
            fetch('/api/patient-ids')
                .then(response => response.json())
                .then(data => {
                    const select = document.getElementById('patientSelect');
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
                    select.addEventListener('change', function() {
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
                        }
                    });
                })
                .catch(error => {
                    console.error('Error loading patient IDs:', error);
                    const t = translations[currentLang];
                    document.getElementById('patientSelect').innerHTML = 
                        `<option value="">${t.errorLoading}</option>`;
                });
        }
        
        loadPatientIDs();
        
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
                        debugText += 'Food Logs Count: ' + data.debug.food_logs_count + '\\n\\n';
                        debugText += 'Complete data available in browser console (F12)\\n';
                        debugText += '\\n- Food Logs Raw Data (from MongoDB)\\n';
                        debugText += '- API Responses (from GET /food-log/{id})\\n';
                        debugText += '\\nOpen browser console to see full details.';
                        
                        alert(debugText);
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
        
        try:
            # Get patient info
            patient_info = get_patient_info(client, patient_id, DATABASE_NAME)
            
            # Query food logs
            food_logs_df = query_food_logs(
                client,
                [patient_id],
                database_name=DATABASE_NAME,
                start_date=start_date,
                end_date=end_date
            )
            
            if food_logs_df.empty:
                client.close()
                return jsonify({
                    "error": f"未找到病人 {patient_id} 在 {date_str} 的食物日志记录"
                }), 404
            
            # Setup images directory
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            
            # Download images if session token is provided
            # Note: This will also try to extract images from MongoDB images field
            if debug:
                print(f"[DEBUG] Before image download - ImgName column exists: {'ImgName' in food_logs_df.columns}")
                if 'images' in food_logs_df.columns:
                    print(f"[DEBUG] Sample images field: {food_logs_df.iloc[0]['images'] if not food_logs_df.empty else 'N/A'}")
            
            # Get image URLs from API
            api_responses = {}
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
            
            # Group by meal type
            food_logs_by_meal = group_food_logs_by_meal(food_logs_df, language=language)
            
            # Prepare debug info if requested
            debug_info = {}
            if debug:
                # Convert DataFrame to list of dicts for JSON serialization
                food_logs_raw = []
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
                    "food_logs_count": len(food_logs_df),
                    "food_logs_raw": food_logs_raw,  # Complete raw food log data from MongoDB
                    "api_responses": api_responses  # Complete API responses for each food log
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
                language=language
            )
            
            client.close()
            
            response = {
                "success": True,
                "html": html_content
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


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

