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
                    "earliest_date": {"$min": "$createdAt"}
                }
            },
            {
                "$sort": {"_id": -1}  # 倒序排列，最新的在最前面
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        
        patient_list = []
        for doc in results:
            member_id = doc["_id"]
            # Convert ObjectId to string if needed
            if isinstance(member_id, ObjectId):
                member_id = str(member_id)
            
            latest_date = doc.get("latest_date")
            earliest_date = doc.get("earliest_date")
            
            # Convert datetime to ISO format string if present
            if latest_date and isinstance(latest_date, datetime):
                latest_date = latest_date.isoformat()
            elif latest_date:
                latest_date = str(latest_date)
            
            if earliest_date and isinstance(earliest_date, datetime):
                earliest_date = earliest_date.isoformat()
            elif earliest_date:
                earliest_date = str(earliest_date)
            
            # Convert datetime to ISO format string if present
            latest_date = doc.get("latest_date")
            earliest_date = doc.get("earliest_date")
            
            if latest_date and isinstance(latest_date, datetime):
                latest_date = latest_date.isoformat()
            elif latest_date:
                latest_date = str(latest_date)
            
            if earliest_date and isinstance(earliest_date, datetime):
                earliest_date = earliest_date.isoformat()
            elif earliest_date:
                earliest_date = str(earliest_date)
            
            patient_list.append({
                "patient_id": str(member_id),
                "food_log_count": doc.get("count", 0),
                "latest_date": latest_date,
                "earliest_date": earliest_date
            })
        
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
        select {
            width: 100%;
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
        }
        input[type="date"] {
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
        }
        .language-switch .lang-option.active {
            background: #4a90e2 !important;
            color: white !important;
        }
        .language-switch .lang-option:not(.active) {
            background: white !important;
            color: #4a90e2 !important;
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
                </div>
            </div>
            
            <div class="form-group">
                <label for="dateSelect" id="dateLabel">选择日期:</label>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <input type="date" id="dateSelect" name="date" required style="flex: 1; max-width: 200px;"/>
                    <div id="datesWithFoodLogs" style="font-size: 12px; color: #666; cursor: pointer; display: none;" title="点击查看有食物日志的日期">
                        <span id="datesIndicator" style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #4a90e2; margin-right: 5px;"></span>
                        <span id="datesCount">0</span>
                    </div>
                    <div class="language-switch" id="languageSwitch" style="display: flex; border: 2px solid #4a90e2; border-radius: 6px; overflow: hidden; cursor: pointer; user-select: none;">
                        <span class="lang-option active" data-lang="zh" style="padding: 6px 12px; background: #4a90e2; color: white; font-size: 13px; font-weight: 600;">中</span>
                        <span class="lang-option" data-lang="en" style="padding: 6px 12px; background: white; color: #4a90e2; font-size: 13px; font-weight: 600;">EN</span>
                    </div>
                </div>
                <div id="datesList" style="display: none; margin-top: 8px; padding: 10px; background: #f8f9fa; border-radius: 4px; font-size: 12px; max-height: 150px; overflow-y: auto;">
                    <div style="font-weight: 600; margin-bottom: 5px;" id="datesListTitle">有食物日志的日期:</div>
                    <div id="datesListContent"></div>
                </div>
            </div>
            
            <button type="submit" id="generateBtn">生成总结</button>
        </form>
        
        <div class="loading" id="loading">
            <div class="spinner"></div>
            <div>正在生成食物日志总结，请稍候...</div>
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
                language: 'Language / 语言:',
                generate: '生成总结',
                loading: '正在生成食物日志总结，请稍候...',
                resultTitle: '生成的总结',
                foodLogCount: '食物日志数量:',
                earliestDate: '最早日期:',
                latestDate: '最晚日期:',
                pleaseSelect: '请选择病人 ID...',
                loadingText: '加载中...',
                errorLoading: '加载失败，请刷新页面重试',
                errorSelect: '请选择病人 ID 和日期',
                records: '条记录',
                datesWithFoodLogs: '有食物日志的日期:'
            },
            en: {
                title: 'Food Log Summary Generator',
                selectPatient: 'Select Patient ID:',
                selectDate: 'Select Date:',
                language: 'Language / 语言:',
                generate: 'Generate Summary',
                loading: 'Generating food log summary, please wait...',
                resultTitle: 'Generated Summary',
                foodLogCount: 'Food Log Count:',
                earliestDate: 'Earliest Date:',
                latestDate: 'Latest Date:',
                pleaseSelect: 'Please select Patient ID...',
                loadingText: 'Loading...',
                errorLoading: 'Failed to load, please refresh and try again',
                errorSelect: 'Please select Patient ID and Date',
                records: ' records',
                datesWithFoodLogs: 'Dates with food logs:'
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
            if (typeof updateDatesListTitle === 'function') {
                updateDatesListTitle();
            }
            document.getElementById('generateBtn').textContent = t.generate;
            document.querySelector('#loading div:last-child').textContent = t.loading;
            document.getElementById('resultTitle').textContent = t.resultTitle;
            
            const patientInfo = document.getElementById('patientInfo');
            if (patientInfo) {
                const foodLogCountEl = document.getElementById('foodLogCount');
                const earliestDateEl = document.getElementById('earliestDate');
                const latestDateEl = document.getElementById('latestDate');
                const count = foodLogCountEl ? foodLogCountEl.textContent : '-';
                const earliest = earliestDateEl ? earliestDateEl.textContent : '-';
                const latest = latestDateEl ? latestDateEl.textContent : '-';
                
                document.getElementById('foodLogCountLabel').innerHTML = `${t.foodLogCount} <span id="foodLogCount">${count}</span>`;
                document.getElementById('earliestDateLabel').innerHTML = `${t.earliestDate} <span id="earliestDate">${earliest}</span>`;
                document.getElementById('latestDateLabel').innerHTML = `${t.latestDate} <span id="latestDate">${latest}</span>`;
            }
            
            // Update patient select placeholder
            const select = document.getElementById('patientSelect');
            if (select.options.length > 0 && !select.value) {
                select.options[0].textContent = t.pleaseSelect;
            }
        }
        
        // Language switch click handler
        document.querySelectorAll('.lang-option').forEach(option => {
            option.addEventListener('click', function() {
                const lang = this.dataset.lang;
                if (lang !== currentLang) {
                    currentLang = lang;
                    localStorage.setItem('language', lang);
                    
                    // Update active state
                    document.querySelectorAll('.lang-option').forEach(opt => {
                        opt.classList.remove('active');
                    });
                    this.classList.add('active');
                    
                    updateLanguage(lang);
                    // Reload patient IDs to update text
                    loadPatientIDs();
                    
                    // Reload dates with food logs if patient is selected
                    const patientId = document.getElementById('patientSelect').value;
                    if (patientId) {
                        loadDatesWithFoodLogs(patientId);
                    }
                }
            });
        });
        
        // Initialize language
        document.querySelector(`.lang-option[data-lang="${currentLang}"]`).classList.add('active');
        updateLanguage(currentLang);
        
        // Set today as default date
        document.getElementById('dateSelect').valueAsDate = new Date();
        
        // Load patient IDs
        function loadPatientIDs() {
            const t = translations[currentLang];
            fetch('/api/patient-ids')
                .then(response => response.json())
                .then(data => {
                    const select = document.getElementById('patientSelect');
                    select.innerHTML = `<option value="">${t.loadingText}</option>`;
                    
                    if (data.error) {
                        select.innerHTML = `<option value="">${t.errorLoading}: ${data.error}</option>`;
                        return;
                    }
                    
                    select.innerHTML = `<option value="">${t.pleaseSelect}</option>`;
                    data.patients.forEach(patient => {
                        const option = document.createElement('option');
                        option.value = patient.patient_id;
                        option.textContent = `${patient.patient_id} (${patient.food_log_count} ${t.records})`;
                        option.dataset.count = patient.food_log_count;
                        option.dataset.earliest = patient.earliest_date || '';
                        option.dataset.latest = patient.latest_date || '';
                        select.appendChild(option);
                    });
                    
                    // Update patient info when selection changes
                    select.addEventListener('change', function() {
                        const option = this.options[this.selectedIndex];
                        if (option.value) {
                            document.getElementById('foodLogCount').textContent = option.dataset.count;
                            const locale = currentLang === 'zh' ? 'zh-CN' : 'en-US';
                            document.getElementById('earliestDate').textContent = 
                                option.dataset.earliest ? new Date(option.dataset.earliest).toLocaleDateString(locale) : '-';
                            document.getElementById('latestDate').textContent = 
                                option.dataset.latest ? new Date(option.dataset.latest).toLocaleDateString(locale) : '-';
                            document.getElementById('patientInfo').style.display = 'block';
                            
                            // Load dates with food logs for this patient
                            loadDatesWithFoodLogs(option.value);
                        } else {
                            document.getElementById('patientInfo').style.display = 'none';
                            document.getElementById('datesWithFoodLogs').style.display = 'none';
                            document.getElementById('datesList').style.display = 'none';
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
        
        // Load dates with food logs for selected patient
        function loadDatesWithFoodLogs(patientId) {
            const t = translations[currentLang];
            fetch(`/api/patient-dates?patient_id=${patientId}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success && data.dates && data.dates.length > 0) {
                        document.getElementById('datesCount').textContent = data.dates.length;
                        document.getElementById('datesWithFoodLogs').style.display = 'inline-block';
                        
                        // Populate dates list
                        const datesListContent = document.getElementById('datesListContent');
                        datesListContent.innerHTML = '';
                        data.dates.forEach(dateStr => {
                            const dateItem = document.createElement('div');
                            dateItem.style.cssText = 'padding: 4px 0; cursor: pointer; color: #4a90e2;';
                            dateItem.textContent = dateStr;
                            dateItem.addEventListener('click', function() {
                                document.getElementById('dateSelect').value = dateStr;
                                document.getElementById('datesList').style.display = 'none';
                            });
                            datesListContent.appendChild(dateItem);
                        });
                    } else {
                        document.getElementById('datesWithFoodLogs').style.display = 'none';
                    }
                })
                .catch(error => {
                    console.error('Error loading dates:', error);
                    document.getElementById('datesWithFoodLogs').style.display = 'none';
                });
        }
        
        // Toggle dates list
        document.getElementById('datesWithFoodLogs').addEventListener('click', function() {
            const datesList = document.getElementById('datesList');
            datesList.style.display = datesList.style.display === 'none' ? 'block' : 'none';
        });
        
        // Update dates list title based on language
        function updateDatesListTitle() {
            const t = translations[currentLang];
            document.getElementById('datesListTitle').textContent = t.datesWithFoodLogs || '有食物日志的日期:';
        }
        
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
                const response = await fetch('/api/generate-summary', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        patient_id: patientId,
                        date: date,
                        language: currentLang
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


@app.route('/api/patient-dates')
def api_patient_dates():
    """API endpoint to get all dates that have food logs for a specific patient."""
    try:
        patient_id = request.args.get('patient_id')
        if not patient_id:
            return jsonify({
                "success": False,
                "error": "Missing patient_id parameter"
            }), 400
        
        client = get_mongo_client(MONGO_URI)
        db = client[DATABASE_NAME]
        collection = db["food_logs"]
        
        # Query distinct dates for this patient
        pipeline = [
            {
                "$match": {
                    "memberId": patient_id
                }
            },
            {
                "$group": {
                    "_id": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$createdAt"
                        }
                    }
                }
            },
            {
                "$sort": {"_id": -1}  # Latest first
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        dates = [doc["_id"] for doc in results]
        
        client.close()
        
        return jsonify({
            "success": True,
            "dates": dates
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
        
        if not patient_id or not date_str:
            return jsonify({"error": "Missing patient_id or date"}), 400
        
        # Parse date
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
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
            
            # Get image URLs from API
            if SESSION_TOKEN:
                food_logs_df, _ = get_food_log_image_urls(
                    food_logs_df,
                    SESSION_TOKEN,
                    debug=False
                )
            
            # Group by meal type
            food_logs_by_meal = group_food_logs_by_meal(food_logs_df)
            
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

