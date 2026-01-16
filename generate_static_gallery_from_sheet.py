#!/usr/bin/env python3
"""
Generate static HTML gallery from Google Sheet with direct Google Sheets API write access
从 Google Sheet 生成静态 HTML 画廊，支持直接通过 Google Sheets API 写入

This script:
1. Reads data from Google Sheet using Google Sheets API
2. Generates static HTML with embedded images
3. Includes JavaScript for direct Google Sheets API write access (OAuth 2.0)

此脚本：
1. 使用 Google Sheets API 从 Google Sheet 读取数据
2. 生成包含嵌入图片的静态 HTML
3. 包含 JavaScript 用于直接通过 Google Sheets API 写入（OAuth 2.0）
"""
import argparse
import base64
import json
import os
import pickle
import re
import sys
import html as html_module
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Tuple

import pandas as pd

# Google API imports
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[ERROR] Missing Google API packages. Please install:", file=sys.stderr)
    print("  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib", file=sys.stderr)
    sys.exit(1)

# Import gallery generation functions from server_review.py
# 从 server_review.py 导入画廊生成函数
from server_review import (
    looks_like_json,
    humanize_json,
    format_rd_comments,
    format_ingredients,
    format_field_value,
    read_image_as_data_uri,
    get_display_columns,
    format_field_name,
    _build_collapsible_raw_data,
    build_card_html,
    build_html
)

# Google API configuration
# Google API 配置
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
TOKEN_FILE = 'google_api_token.pickle'
CREDENTIALS_FILE = 'credentials.json'


def get_credentials():
    """Get valid user credentials from storage or create new ones."""
    creds = None
    
    # Load existing token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)
    
    # If no valid credentials, authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"[ERROR] {CREDENTIALS_FILE} not found!", file=sys.stderr)
                print(f"Please download credentials.json from Google Cloud Console", file=sys.stderr)
                print(f"Enable Google Sheets API", file=sys.stderr)
                sys.exit(1)
            
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save credentials for next run
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)
    
    return creds


def read_sheet_as_dataframe(spreadsheet_id: str, sheet_name: str = None) -> pd.DataFrame:
    """Read Google Sheet data into pandas DataFrame."""
    creds = get_credentials()
    sheets_service = build('sheets', 'v4', credentials=creds)
    
    try:
        # First, get the spreadsheet metadata to find the actual sheet name
        # 首先，获取 spreadsheet 元数据以找到实际的 sheet 名称
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = spreadsheet.get('sheets', [])
        
        if not sheets:
            raise Exception("Spreadsheet has no sheets")
        
        # If sheet_name is provided, try to find it; otherwise use the first sheet
        # 如果提供了 sheet_name，尝试找到它；否则使用第一个 sheet
        actual_sheet_name = None
        if sheet_name:
            for sheet in sheets:
                if sheet['properties']['title'] == sheet_name:
                    actual_sheet_name = sheet_name
                    break
            if not actual_sheet_name:
                # Sheet name not found, use first available and warn
                # Sheet 名称未找到，使用第一个可用的并警告
                available_sheets = [s['properties']['title'] for s in sheets]
                print(f"[WARN] Sheet '{sheet_name}' not found. Using first available sheet: {sheets[0]['properties']['title']}", file=sys.stderr)
                print(f"[INFO] Available sheets: {', '.join(available_sheets)}", file=sys.stderr)
                actual_sheet_name = sheets[0]['properties']['title']
        else:
            # Use first sheet if no name provided
            actual_sheet_name = sheets[0]['properties']['title']
        
        print(f"[INFO] Using sheet: {actual_sheet_name}")
        
        # Read all data from the sheet
        # Use a simpler range format that Google Sheets API prefers
        # 使用 Google Sheets API 更喜欢的简单范围格式
        range_name = f"'{actual_sheet_name}'!A1:ZZ1000"
        
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()
        
        values = result.get('values', [])
        if not values:
            raise Exception("Sheet is empty")
        
        # First row is headers
        headers = values[0]
        
        # Create DataFrame
        rows = []
        for row in values[1:]:
            # Pad row to match header length
            padded_row = row + [''] * (len(headers) - len(row))
            rows.append(padded_row[:len(headers)])
        
        df = pd.DataFrame(rows, columns=headers)
        return df
        
    except HttpError as e:
        error_details = str(e)
        if hasattr(e, 'content'):
            try:
                error_json = json.loads(e.content.decode('utf-8'))
                error_details = error_json.get('error', {}).get('message', error_details)
            except:
                pass
        raise Exception(f"Failed to read Google Sheet: {error_details}")
    except Exception as e:
        raise Exception(f"Error reading Google Sheet: {e}")


def get_client_id_from_credentials() -> str:
    """Extract client_id from credentials.json for OAuth 2.0.
    Prefers 'web' type over 'installed' type for browser-based OAuth.
    """
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            creds_data = json.load(f)
            # Prefer 'web' type for browser-based OAuth (required for static HTML)
            # 优先使用 'web' 类型用于基于浏览器的 OAuth（静态 HTML 需要）
            if 'web' in creds_data:
                client_id = creds_data['web']['client_id']
                print(f"[INFO] Using 'web' type OAuth client ID", file=sys.stderr)
                return client_id
            elif 'installed' in creds_data:
                client_id = creds_data['installed']['client_id']
                print(f"[WARN] Using 'installed' type OAuth client ID. This may not work for browser-based OAuth.", file=sys.stderr)
                print(f"[WARN] Please create a 'Web application' type OAuth 2.0 client in Google Cloud Console.", file=sys.stderr)
                return client_id
            else:
                print(f"[ERROR] No 'web' or 'installed' client found in credentials.json", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] Could not extract client_id from credentials.json: {e}", file=sys.stderr)
    return None


def generate_static_gallery_html(spreadsheet_id: str, sheet_name: str = None, images_dir: Path = None, client_id: str = None) -> str:
    """Generate static HTML gallery from Google Sheet data."""
    try:
        # Get actual sheet name (will be determined in read_sheet_as_dataframe if not provided)
        # 获取实际的 sheet 名称（如果未提供，将在 read_sheet_as_dataframe 中确定）
        creds = get_credentials()
        sheets_service = build('sheets', 'v4', credentials=creds)
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = spreadsheet.get('sheets', [])
        
        if not sheets:
            raise Exception("Spreadsheet has no sheets")
        
        # Determine actual sheet name
        # 确定实际的 sheet 名称
        actual_sheet_name = None
        if sheet_name:
            # Try to find the specified sheet
            # 尝试找到指定的 sheet
            for sheet in sheets:
                if sheet['properties']['title'] == sheet_name:
                    actual_sheet_name = sheet_name
                    break
            if not actual_sheet_name:
                # Sheet not found, use first available and warn
                # Sheet 未找到，使用第一个可用的并警告
                available_sheets = [s['properties']['title'] for s in sheets]
                print(f"[WARN] Sheet '{sheet_name}' not found. Using first available sheet: {sheets[0]['properties']['title']}", file=sys.stderr)
                print(f"[INFO] Available sheets: {', '.join(available_sheets)}", file=sys.stderr)
                actual_sheet_name = sheets[0]['properties']['title']
        else:
            # No sheet name provided, use first sheet
            # 未提供 sheet 名称，使用第一个 sheet
            actual_sheet_name = sheets[0]['properties']['title']
        
        print(f"[INFO] Using sheet: {actual_sheet_name}")
        
        # Read data from Google Sheet
        print(f"[INFO] Reading data from Google Sheet: {spreadsheet_id}")
        df = read_sheet_as_dataframe(spreadsheet_id, actual_sheet_name)
        
        if "ImgName" not in df.columns:
            return f"<html><body><h1>Error</h1><p>Google Sheet does not have ImgName column</p></body></html>"
        
        display_columns = get_display_columns(df)
        cards_html = []
        
        for idx, row in df.iterrows():
            try:
                cards_html.append(build_card_html(row, images_dir, display_columns, row_idx=idx))
            except Exception as e:
                print(f"[WARN] Failed to render row {idx}: {e}", file=sys.stderr)
                continue
        
        # Build HTML
        html_content = build_html("".join(cards_html), title="FoodLog Gallery - RD Feedback")
        
        # Get client_id if not provided
        if not client_id:
            client_id = get_client_id_from_credentials()
            if not client_id:
                print("[WARN] Client ID not found. Feedback submission may not work.", file=sys.stderr)
                client_id = 'YOUR_CLIENT_ID_HERE'
        
        # Inject Google Sheets API configuration and OAuth 2.0 implementation
        # 注入 Google Sheets API 配置和 OAuth 2.0 实现
        api_config_script = f"""
<script src="https://apis.google.com/js/api.js"></script>
<script src="https://accounts.google.com/gsi/client"></script>
<script>
// Google Sheets API Configuration
// Google Sheets API 配置
const GOOGLE_SHEET_ID = '{spreadsheet_id}';
const SHEET_NAME = '{actual_sheet_name}';
const CLIENT_ID = '{client_id}';
const DISCOVERY_DOCS = ['https://sheets.googleapis.com/$discovery/rest?version=v4'];
const SCOPES = 'https://www.googleapis.com/auth/spreadsheets';

let tokenClient;
let accessToken = null;
let gapiInitialized = false;

// Token storage keys
// Token 存储键
const TOKEN_STORAGE_KEY = 'google_sheets_access_token';
const TOKEN_EXPIRY_KEY = 'google_sheets_token_expiry';

// Load saved token from localStorage
// 从 localStorage 加载保存的 token
// Note: localStorage works on GitHub Pages (same origin: https://chengyao-ihealth.github.io)
// 注意：localStorage 在 GitHub Pages 上可以正常工作（同源：https://chengyao-ihealth.github.io）
function loadSavedToken() {{
    try {{
        // Check if localStorage is available (should be available on GitHub Pages)
        // 检查 localStorage 是否可用（在 GitHub Pages 上应该可用）
        if (typeof Storage === 'undefined' || !window.localStorage) {{
            console.warn('[WARN] localStorage is not available');
            return false;
        }}
        
        const savedToken = localStorage.getItem(TOKEN_STORAGE_KEY);
        const savedExpiry = localStorage.getItem(TOKEN_EXPIRY_KEY);
        
        if (savedToken && savedExpiry) {{
            const expiryTime = parseInt(savedExpiry, 10);
            const now = Date.now();
            
            // Check if token is still valid (with 5 minute buffer)
            // 检查 token 是否仍然有效（保留 5 分钟缓冲）
            if (now < expiryTime - 5 * 60 * 1000) {{
                accessToken = savedToken;
                console.log('[DEBUG] Loaded saved access token from localStorage');
                return true;
            }} else {{
                console.log('[DEBUG] Saved token expired, clearing...');
                localStorage.removeItem(TOKEN_STORAGE_KEY);
                localStorage.removeItem(TOKEN_EXPIRY_KEY);
            }}
        }}
    }} catch (e) {{
        // Handle cases where localStorage might be blocked (e.g., private browsing)
        // 处理 localStorage 可能被阻止的情况（例如，隐私浏览模式）
        console.warn('[WARN] Failed to load saved token:', e);
        // localStorage might be disabled, continue without saved token
        // localStorage 可能被禁用，继续不使用保存的 token
    }}
    return false;
}}

// Save token to localStorage
// 保存 token 到 localStorage
// Works on GitHub Pages: localStorage is domain-specific (https://chengyao-ihealth.github.io)
// 在 GitHub Pages 上可用：localStorage 是域名特定的（https://chengyao-ihealth.github.io）
function saveToken(token, expiresIn) {{
    try {{
        // Check if localStorage is available
        // 检查 localStorage 是否可用
        if (typeof Storage === 'undefined' || !window.localStorage) {{
            console.warn('[WARN] localStorage is not available, token will not be saved');
            return;
        }}
        
        localStorage.setItem(TOKEN_STORAGE_KEY, token);
        // Calculate expiry time (expiresIn is in seconds)
        // 计算过期时间（expiresIn 以秒为单位）
        const expiryTime = Date.now() + (expiresIn * 1000);
        localStorage.setItem(TOKEN_EXPIRY_KEY, expiryTime.toString());
        console.log('[DEBUG] Saved access token to localStorage (works on GitHub Pages)');
    }} catch (e) {{
        // Handle cases where localStorage might be blocked (e.g., private browsing, quota exceeded)
        // 处理 localStorage 可能被阻止的情况（例如，隐私浏览模式、配额超出）
        console.warn('[WARN] Failed to save token to localStorage:', e);
        // Continue without saving - user will need to re-authorize next time
        // 继续但不保存 - 用户下次需要重新授权
    }}
}}

// Initialize Google API
// 初始化 Google API
function initGoogleAPI() {{
    if (gapiInitialized) return;
    
    console.log('[DEBUG] Starting Google API initialization...');
    
    // Try to load saved token first
    // 首先尝试加载保存的 token
    loadSavedToken();
    
    // Initialize Google Identity Services first (doesn't require API init)
    // 首先初始化 Google Identity Services（不需要 API 初始化）
    if (typeof google !== 'undefined' && google.accounts) {{
        tokenClient = google.accounts.oauth2.initTokenClient({{
            client_id: CLIENT_ID,
            scope: SCOPES,
            callback: (response) => {{
                if (response.error) {{
                    console.error('[ERROR] OAuth error:', response.error);
                    return;
                }}
                accessToken = response.access_token;
                // Save token to localStorage
                // 保存 token 到 localStorage
                if (response.expires_in) {{
                    saveToken(accessToken, response.expires_in);
                }}
                if (gapiInitialized) {{
                    gapi.client.setToken({{ access_token: accessToken }});
                }}
                console.log('[DEBUG] Access token obtained and saved');
            }},
        }});
        console.log('[DEBUG] Google Identity Services initialized');
    }} else {{
        console.error('[ERROR] Google Identity Services not available');
    }}
    
    // Initialize Google API client
    // 初始化 Google API 客户端
    if (typeof gapi !== 'undefined') {{
        gapi.load('client', () => {{
            console.log('[DEBUG] gapi.load completed, initializing client...');
            gapi.client.init({{
                discoveryDocs: DISCOVERY_DOCS,
            }}).then(() => {{
                console.log('[DEBUG] Google API client initialized successfully');
                gapiInitialized = true;
                // If we already have a token, set it now
                // 如果已经有 token，现在设置它
                if (accessToken) {{
                    gapi.client.setToken({{ access_token: accessToken }});
                }}
            }}).catch(err => {{
                console.error('[ERROR] Failed to initialize Google API:', err);
                console.error('[ERROR] Error details:', err.message, err.stack);
                // Try to set initialized anyway if it's just a discovery doc issue
                // 如果只是 discovery doc 问题，尝试仍然设置初始化
                gapiInitialized = true;
            }});
        }}).catch(err => {{
            console.error('[ERROR] Failed to load Google API:', err);
        }});
    }} else {{
        console.error('[ERROR] gapi is not defined');
    }}
}}

// Request access token
// 请求访问令牌
function requestAccessToken() {{
    if (!accessToken) {{
        tokenClient.requestAccessToken({{ prompt: 'consent' }});
    }}
}}

// Function to get column letter from index (0-based)
// 从索引获取列字母（0-based）
function getColumnLetter(columnIndex) {{
    let result = '';
    while (columnIndex >= 0) {{
        result = String.fromCharCode(65 + (columnIndex % 26)) + result;
        columnIndex = Math.floor(columnIndex / 26) - 1;
    }}
    return result;
}}

// Function to find column index by name
// 通过名称查找列索引
async function findColumnIndex(columnName) {{
    try {{
        const response = await gapi.client.sheets.spreadsheets.values.get({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: `${{SHEET_NAME}}!1:1`,
        }});
        
        const headers = response.result.values[0] || [];
        return headers.indexOf(columnName);
    }} catch (error) {{
        console.error('Error finding column:', error);
        return -1;
    }}
}}

// Function to create a new column if it doesn't exist
// 如果列不存在则创建新列
async function createColumnIfNotExists(columnName) {{
    try {{
        // First check if column exists
        // 首先检查列是否存在
        let colIdx = await findColumnIndex(columnName);
        if (colIdx !== -1) {{
            console.log(`[DEBUG] Column '${{columnName}}' already exists at index ${{colIdx}}`);
            return colIdx;
        }}
        
        console.log(`[DEBUG] Column '${{columnName}}' not found, creating it...`);
        
        // Get headers to find the last column
        // 获取表头以找到最后一列
        const response = await gapi.client.sheets.spreadsheets.values.get({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: `${{SHEET_NAME}}!1:1`,
        }});
        
        const headers = response.result.values[0] || [];
        const newColIdx = headers.length;
        const colLetter = getColumnLetter(newColIdx);
        
        // Add the new column header
        // 添加新列的表头
        await gapi.client.sheets.spreadsheets.values.update({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: `${{SHEET_NAME}}!${{colLetter}}1`,
            valueInputOption: 'RAW',
            values: [[columnName]],
        }});
        
        console.log(`[DEBUG] Created column '${{columnName}}' at index ${{newColIdx}} (column ${{colLetter}})`);
        return newColIdx;
        
    }} catch (error) {{
        console.error(`[ERROR] Failed to create column '${{columnName}}':`, error);
        throw error;
    }}
}}

// Function to find row index by FoodLogId
// 通过 FoodLogId 查找行索引
async function findRowIndex(foodlogId) {{
    try {{
        const response = await gapi.client.sheets.spreadsheets.values.get({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: `${{SHEET_NAME}}!A:ZZ`,
        }});
        
        const values = response.result.values || [];
        if (values.length === 0) return -1;
        
        // Find FoodLogId column
        const headers = values[0];
        const foodlogIdCol = headers.indexOf('FoodLogId');
        if (foodlogIdCol === -1) return -1;
        
        // Find the row with matching FoodLogId
        for (let i = 1; i < values.length; i++) {{
            if (values[i][foodlogIdCol] === foodlogId) {{
                return i + 1; // 1-based row index
            }}
        }}
        
        return -1;
    }} catch (error) {{
        console.error('Error finding row:', error);
        return -1;
    }}
}}

// Function to add feedback using Google Sheets API
// 使用 Google Sheets API 添加反馈的函数
async function addFeedbackViaSheetsAPI(foodlogId, rdName, rdFeedback) {{
    console.log('[DEBUG] addFeedbackViaSheetsAPI called', {{ foodlogId, rdName }});
    
    // Check if Google API is initialized
    // 检查 Google API 是否已初始化
    if (!gapiInitialized) {{
        console.log('[DEBUG] Waiting for Google API initialization...');
        console.log('[DEBUG] gapi available:', typeof gapi !== 'undefined');
        console.log('[DEBUG] gapi.client available:', typeof gapi !== 'undefined' && typeof gapi.client !== 'undefined');
        
        let timeout = 0;
        await new Promise((resolve, reject) => {{
            const checkInit = setInterval(() => {{
                timeout += 100;
                // Check if gapi.client is available (even if init hasn't completed)
                // 检查 gapi.client 是否可用（即使 init 尚未完成）
                if (typeof gapi !== 'undefined' && typeof gapi.client !== 'undefined') {{
                    // Try to use it even if not fully initialized
                    // 即使未完全初始化也尝试使用
                    if (gapiInitialized || timeout > 2000) {{
                        clearInterval(checkInit);
                        if (!gapiInitialized) {{
                            console.log('[DEBUG] Proceeding without full initialization (gapi.client is available)');
                            gapiInitialized = true;
                        }}
                        console.log('[DEBUG] Google API ready');
                        resolve();
                    }}
                }} else if (timeout > 15000) {{
                    clearInterval(checkInit);
                    reject(new Error('Google API initialization timeout. gapi.client is not available.'));
                }}
            }}, 100);
        }});
    }}
    
    // Check if we have access token (try loading from localStorage first)
    // 检查是否有访问令牌（首先尝试从 localStorage 加载）
    if (!accessToken) {{
        loadSavedToken();
    }}
    
    if (!accessToken) {{
        console.log('[DEBUG] No access token, requesting authorization...');
        // Request token if not available
        // 如果没有令牌则请求
        return new Promise((resolve, reject) => {{
            let authTimeout = setTimeout(() => {{
                reject(new Error('Authorization timeout. Please check if the authorization popup was blocked.'));
            }}, 60000); // 60 second timeout
            
            tokenClient.callback = (response) => {{
                clearTimeout(authTimeout);
                if (response.error) {{
                    console.error('[ERROR] OAuth error:', response.error);
                    reject(new Error('Authorization failed: ' + response.error));
                    return;
                }}
                console.log('[DEBUG] Access token obtained');
                accessToken = response.access_token;
                // Save token to localStorage
                // 保存 token 到 localStorage
                if (response.expires_in) {{
                    saveToken(accessToken, response.expires_in);
                }}
                gapi.client.setToken({{ access_token: accessToken }});
                addFeedbackViaSheetsAPI(foodlogId, rdName, rdFeedback)
                    .then(resolve)
                    .catch(reject);
            }};
            console.log('[DEBUG] Requesting access token...');
            // Use 'select_account' instead of 'consent' to avoid re-prompting if already authorized
            // 使用 'select_account' 而不是 'consent'，避免在已授权时重新提示
            tokenClient.requestAccessToken({{ prompt: 'select_account' }});
        }});
    }} else {{
        // Token exists, make sure it's set for gapi
        // Token 存在，确保为 gapi 设置
        if (gapiInitialized) {{
            gapi.client.setToken({{ access_token: accessToken }});
        }}
    }}
    
    try {{
        console.log('[DEBUG] Finding row for FoodLogId:', foodlogId);
        // Find the row with matching FoodLogId
        // 找到匹配 FoodLogId 的行
        const rowIdx = await findRowIndex(foodlogId);
        console.log('[DEBUG] Found row index:', rowIdx);
        if (rowIdx === -1) {{
            throw new Error('FoodLogId not found: ' + foodlogId);
        }}
        
        console.log('[DEBUG] Finding RD Feedback column...');
        // Find or create RD Feedback column
        // 找到或创建 RD Feedback 列
        let rdFeedbackColIdx = await createColumnIfNotExists('RD Feedback');
        console.log('[DEBUG] RD Feedback column index:', rdFeedbackColIdx);
        
        // Read current feedback value
        // 读取当前反馈值
        const colLetter = getColumnLetter(rdFeedbackColIdx);
        console.log('[DEBUG] Reading current feedback from:', `${{SHEET_NAME}}!${{colLetter}}${{rowIdx}}`);
        let currentValue = '';
        
        try {{
            const readResponse = await gapi.client.sheets.spreadsheets.values.get({{
                spreadsheetId: GOOGLE_SHEET_ID,
                range: `${{SHEET_NAME}}!${{colLetter}}${{rowIdx}}`,
            }});
            
            if (readResponse.result.values && readResponse.result.values[0]) {{
                currentValue = readResponse.result.values[0][0] || '';
            }}
            console.log('[DEBUG] Current feedback value:', currentValue.substring(0, 100));
        }} catch (e) {{
            console.log('[DEBUG] Cell is empty or read failed:', e);
            // Cell might be empty
            currentValue = '';
        }}
        
        // Parse existing feedbacks
        // 解析现有反馈
        let feedbackList = [];
        if (currentValue && currentValue.trim()) {{
            try {{
                const feedbackStr = currentValue.trim();
                if (feedbackStr.startsWith('[')) {{
                    feedbackList = JSON.parse(feedbackStr);
                }} else if (feedbackStr.startsWith('{{')) {{
                    feedbackList = [JSON.parse(feedbackStr)];
                }}
                console.log('[DEBUG] Parsed', feedbackList.length, 'existing feedbacks');
            }} catch (e) {{
                console.log('[DEBUG] Failed to parse existing feedbacks, starting fresh');
                // If parsing fails, start with empty list
                feedbackList = [];
            }}
        }}
        
        // Add new feedback
        // 添加新反馈
        feedbackList.push({{
            rd_name: rdName,
            feedback: rdFeedback,
            feedbackedAt: new Date().toISOString()
        }});
        console.log('[DEBUG] Adding new feedback, total:', feedbackList.length);
        
        // Write back to sheet
        // 写回 sheet
        console.log('[DEBUG] Writing to sheet...');
        await gapi.client.sheets.spreadsheets.values.update({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: `${{SHEET_NAME}}!${{colLetter}}${{rowIdx}}`,
            valueInputOption: 'RAW',
            values: [[JSON.stringify(feedbackList)]],
        }});
        
        console.log('[DEBUG] Feedback written successfully');
        return {{ success: true, message: 'Feedback added successfully' }};
        
    }} catch (error) {{
        console.error('[ERROR] Error adding feedback:', error);
        console.error('[ERROR] Error details:', error.message, error.stack);
        return {{ success: false, error: error.message || 'Unknown error' }};
    }}
}}

// Initialize on page load
// 页面加载时初始化
document.addEventListener('DOMContentLoaded', function() {{
    console.log('[DEBUG] Page loaded, initializing Google API...');
    console.log('[DEBUG] CLIENT_ID:', CLIENT_ID);
    console.log('[DEBUG] GOOGLE_SHEET_ID:', GOOGLE_SHEET_ID);
    console.log('[DEBUG] SHEET_NAME:', SHEET_NAME);
    
    if (!CLIENT_ID || CLIENT_ID === 'YOUR_CLIENT_ID_HERE') {{
        console.error('[ERROR] CLIENT_ID is not configured!');
    }}
    
    initGoogleAPI();
    
    // Check if Google API libraries are loaded
    // 检查 Google API 库是否已加载
    setTimeout(() => {{
        if (typeof gapi === 'undefined') {{
            console.error('[ERROR] Google API (gapi) not loaded. Check if the script tag is correct.');
        }} else {{
            console.log('[DEBUG] Google API (gapi) loaded');
        }}
        
        if (typeof google === 'undefined' || !google.accounts) {{
            console.error('[ERROR] Google Identity Services not loaded. Check if the script tag is correct.');
        }} else {{
            console.log('[DEBUG] Google Identity Services loaded');
        }}
    }}, 1000);
}});
</script>
"""
        
        # Replace the fetch call in the existing JavaScript
        # 替换现有 JavaScript 中的 fetch 调用
        import re
        
        # Pattern to match the fetch call and response.json()
        # 匹配 fetch 调用和 response.json() 的模式
        old_fetch_pattern = (
            r"const response = await fetch\('/api/add-review', \{"
            r"[^}]+method: 'POST',"
            r"[^}]+headers: \{"
            r"[^}]+'Content-Type': 'application/json',"
            r"[^}]+\},"
            r"[^}]+body: JSON\.stringify\(\{"
            r"[^}]+\}\)"
            r"[^}]+\}\);"
            r"\s+const result = await response\.json\(\);"
        )
        
        new_fetch_code = """console.log('[DEBUG] Form submitted, calling addFeedbackViaSheetsAPI...');
                const result = await addFeedbackViaSheetsAPI(foodlogId, rdName, rdFeedback);
                console.log('[DEBUG] Result received:', result);
                const response = { ok: result.success, json: async () => result };"""
        
        html_content = re.sub(old_fetch_pattern, new_fetch_code, html_content, flags=re.DOTALL)
        
        # Also add error handling improvements to the catch block
        # 同时改进 catch 块的错误处理
        html_content = html_content.replace(
            "} catch (error) {",
            "} catch (error) {\n                console.error('[ERROR] Exception during submission:', error);\n                console.error('[ERROR] Error stack:', error.stack);"
        )
        
        # Improve error message display
        # 改进错误消息显示
        html_content = html_content.replace(
            "statusDiv.textContent = 'Submission failed: ' + error.message;",
            "statusDiv.textContent = 'Submission failed: ' + (error.message || 'Unknown error');\n                console.error('[ERROR] Full error object:', error);"
        )
        
        # Add function to append feedback to display area (instead of reloading page)
        # 添加函数以将反馈追加到显示区域（而不是重新加载页面）
        add_feedback_display_function = """
// Function to add feedback item to the display area
// 将反馈项添加到显示区域的函数
function addFeedbackToDisplay(foodlogId, rdName, rdFeedback) {
    const reviewDisplay = document.getElementById('review-display-' + foodlogId);
    if (!reviewDisplay) {
        console.error('[ERROR] Review display element not found for foodlogId:', foodlogId);
        return;
    }
    
    // Format timestamp
    // 格式化时间戳
    const now = new Date();
    const timestamp = now.getFullYear() + '-' + 
        String(now.getMonth() + 1).padStart(2, '0') + '-' + 
        String(now.getDate()).padStart(2, '0') + ' ' + 
        String(now.getHours()).padStart(2, '0') + ':' + 
        String(now.getMinutes()).padStart(2, '0') + ':' + 
        String(now.getSeconds()).padStart(2, '0');
    
    // Escape HTML to prevent XSS
    // 转义 HTML 以防止 XSS
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    const escapedName = escapeHtml(rdName);
    const escapedFeedback = escapeHtml(rdFeedback).replace(/\\n/g, '<br/>');
    
    // Create new feedback item
    // 创建新的反馈项
    const feedbackItem = document.createElement('div');
    feedbackItem.className = 'review-display-item';
    feedbackItem.innerHTML = 
        '<div class="review-display-header">RD Feedback:</div>' +
        '<div class="review-display-content">' + escapedFeedback + '</div>' +
        '<div class="review-display-meta">By: ' + escapedName + ' | ' + timestamp + '</div>';
    
    // Append to display area
    // 追加到显示区域
    reviewDisplay.appendChild(feedbackItem);
    
    // Make sure display area is visible
    // 确保显示区域可见
    reviewDisplay.style.display = 'block';
    
    console.log('[DEBUG] Feedback added to display area');
}
"""
        
        # Insert the function before the form submission handler
        # 在表单提交处理程序之前插入函数
        html_content = html_content.replace(
            'document.addEventListener(\'DOMContentLoaded\', function() {',
            add_feedback_display_function + '\n\ndocument.addEventListener(\'DOMContentLoaded\', function() {'
        )
        
        # Replace window.location.reload() with immediate display update
        # 用立即显示更新替换 window.location.reload()
        # Find and replace the reload logic in the success handler
        # 在成功处理程序中查找并替换重新加载逻辑
        reload_pattern = (
            r"(statusDiv\.textContent = 'Success! Feedback saved\.';"
            r"\s+statusDiv\.className = 'form-status success';"
            r"\s+// Reload the page to show all feedbacks.*?"
            r"setTimeout\(function\(\) \{\s+window\.location\.reload\(\);\s+\}, 500\);)"
        )
        
        new_success_code = """statusDiv.textContent = 'Success! Feedback saved.';
                    statusDiv.className = 'form-status success';
                    
                    // Add feedback to display immediately without reloading
                    // 立即将反馈添加到显示区域，无需重新加载
                    addFeedbackToDisplay(foodlogId, rdName, rdFeedback);
                    
                    // Clear form
                    // 清空表单
                    form.querySelector('input[name="rd_name"]').value = '';
                    form.querySelector('textarea[name="rd_feedback"]').value = '';"""
        
        html_content = re.sub(reload_pattern, new_success_code, html_content, flags=re.DOTALL)
        
        # Also handle the case where the pattern might be slightly different
        # 同时处理模式可能略有不同的情况
        html_content = html_content.replace(
            "// Reload the page to show all feedbacks (including the new one)",
            "// Add feedback to display immediately without reloading"
        )
        html_content = html_content.replace(
            "setTimeout(function() {\n                        window.location.reload();\n                    }, 500);",
            "addFeedbackToDisplay(foodlogId, rdName, rdFeedback);\n                    form.querySelector('input[name=\"rd_name\"]').value = '';\n                    form.querySelector('textarea[name=\"rd_feedback\"]').value = '';"
        )
        
        # Insert API config before the existing script
        # 在现有脚本之前插入 API 配置
        html_content = html_content.replace(
            '<script>',
            api_config_script + '\n<script>',
            1
        )
        
        return html_content
        
    except Exception as e:
        return f"<html><body><h1>Error</h1><p>Failed to generate gallery: {str(e)}</p></body></html>"


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Generate static HTML gallery from Google Sheet with direct API write access"
    )
    parser.add_argument(
        '--spreadsheet-id',
        default='1ab4zdVUCTvmcM2nOwHLKG3vJ6k3PBaf7ErKrQGH-JJE',
        help='Google Spreadsheet ID'
    )
    parser.add_argument(
        '--sheet-name',
        default='Sheet1',
        help='Sheet tab name (default: Sheet1)'
    )
    parser.add_argument(
        '--client-id',
        default=None,
        help='Google OAuth 2.0 Client ID (will try to extract from credentials.json if not provided)'
    )
    parser.add_argument(
        '--images',
        default='./images',
        help='Images directory (default: ./images)'
    )
    parser.add_argument(
        '--output',
        default='gallery.html',
        help='Output HTML file (default: gallery.html)'
    )
    
    args = parser.parse_args()
    
    spreadsheet_id = args.spreadsheet_id
    sheet_name = args.sheet_name
    images_dir = Path(args.images)
    output_path = Path(args.output)
    
    if not images_dir.exists():
        print(f"[WARN] Images directory does not exist: {images_dir}", file=sys.stderr)
    
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"[ERROR] {CREDENTIALS_FILE} not found!", file=sys.stderr)
        print(f"Please download credentials.json from Google Cloud Console", file=sys.stderr)
        sys.exit(1)
    
    print(f"[INFO] Google Spreadsheet ID: {spreadsheet_id}")
    print(f"[INFO] Sheet name: {sheet_name}")
    print(f"[INFO] Images directory: {images_dir.resolve()}")
    
    # Generate HTML
    # 生成 HTML
    html_content = generate_static_gallery_html(
        spreadsheet_id,
        sheet_name,
        images_dir,
        args.client_id
    )
    
    # Save to file
    # 保存到文件
    output_path.write_text(html_content, encoding='utf-8')
    print(f"[OK] Generated static HTML: {output_path.resolve()}")
    print(f"\n[IMPORTANT] OAuth 2.0 Configuration:")
    print(f"[IMPORTANT] OAuth 2.0 配置：")
    print(f"  - For local development: Add http://localhost and http://127.0.0.1 to authorized JavaScript origins")
    print(f"  - 本地开发：在授权的 JavaScript 源中添加 http://localhost 和 http://127.0.0.1")
    print(f"  - For GitHub Pages: Add https://chengyao-ihealth.github.io to authorized JavaScript origins")
    print(f"  - GitHub Pages：在授权的 JavaScript 源中添加 https://chengyao-ihealth.github.io")
    print(f"\n[INFO] Token Storage:")
    print(f"[INFO] Token 存储：")
    print(f"  - Tokens are saved in browser localStorage (works on GitHub Pages)")
    print(f"  - Token 保存在浏览器 localStorage 中（在 GitHub Pages 上可用）")
    print(f"  - Users only need to authorize once per browser/device")
    print(f"  - 用户每个浏览器/设备只需授权一次")
    print(f"  - Token persists across page refreshes and browser sessions")
    print(f"  - Token 在页面刷新和浏览器会话之间保持有效")
    print(f"\n[INFO] You can now:")
    print(f"  - Open via HTTP server: python3 -m http.server 8000 (then visit http://localhost:8000/{output_path.name})")
    print(f"  - Deploy to GitHub Pages: push to repository and access via https://chengyao-ihealth.github.io/ai-tools/{output_path.name}")
    print(f"\n[INFO] Users will be prompted to authorize Google Sheets access on first use only")


if __name__ == '__main__':
    main()
