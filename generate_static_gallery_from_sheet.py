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


def read_sheet_as_dataframe(spreadsheet_id: str, sheet_name: str = None, api_key: str = None) -> tuple[pd.DataFrame, str]:
    """Read Google Sheet data into pandas DataFrame.
    
    Can use either API key (for public sheets) or OAuth credentials (for private sheets).
    If api_key is provided, uses API key. Otherwise, uses OAuth credentials.
    """
    if api_key:
        # Use API key for public sheets (no OAuth needed)
        # 使用 API key 读取公开的 Sheet（不需要 OAuth）
        sheets_service = build('sheets', 'v4', developerKey=api_key)
    else:
        # Use OAuth credentials for private sheets
        # 使用 OAuth 凭据读取私有的 Sheet
        creds = get_credentials()
        sheets_service = build('sheets', 'v4', credentials=creds)
    
    try:
        # Determine sheet name to use
        # 确定要使用的 sheet 名称
        actual_sheet_name = sheet_name if sheet_name else "Sheet1"
        
        # If using API key and no sheet name specified, read without sheet name (uses first sheet)
        # 如果使用 API key 且未指定 sheet 名称，不使用 sheet 名称读取（使用第一个 sheet）
        if api_key and not sheet_name:
            print(f"[INFO] Using API key without sheet name, will read first sheet", file=sys.stderr)
            actual_sheet_name = ""
        
        # Try to get spreadsheet metadata to verify sheet exists (only if using OAuth)
        # 尝试获取 spreadsheet 元数据以验证 sheet 存在（仅在使用 OAuth 时）
        if not api_key:
            try:
                spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
                sheets = spreadsheet.get('sheets', [])
                if sheets:
                    available_sheets = [s['properties']['title'] for s in sheets]
                    if sheet_name:
                        # Check if specified sheet exists
                        # 检查指定的 sheet 是否存在
                        if sheet_name not in available_sheets:
                            print(f"[WARN] Sheet '{sheet_name}' not found. Using first available sheet: {sheets[0]['properties']['title']}", file=sys.stderr)
                            print(f"[INFO] Available sheets: {', '.join(available_sheets)}", file=sys.stderr)
                            actual_sheet_name = sheets[0]['properties']['title']
                        else:
                            actual_sheet_name = sheet_name
                    else:
                        actual_sheet_name = sheets[0]['properties']['title']
            except Exception as e:
                print(f"[WARN] Could not get sheet metadata, using provided/default name: {actual_sheet_name}", file=sys.stderr)
        
        # Read all data from the sheet
        # 读取 sheet 中的所有数据
        if not actual_sheet_name:
            # No sheet name specified, read without sheet name (uses first sheet)
            # 未指定 sheet 名称，不使用 sheet 名称读取（使用第一个 sheet）
            print(f"[INFO] Reading first sheet without name", file=sys.stderr)
            range_name = "A1:ZZ1000"
        else:
            print(f"[INFO] Using sheet: {actual_sheet_name}")
            # Escape sheet name if it contains special characters
            # 如果 sheet 名称包含特殊字符，需要转义
            # Sheet names with spaces or special chars need to be wrapped in single quotes
            # 包含空格或特殊字符的 sheet 名称需要用单引号包裹
            if ' ' in actual_sheet_name or any(c in actual_sheet_name for c in ['!', '@', '#', '$', '%', '^', '&', '*', '(', ')', '-', '+', '=', '[', ']', '{', '}', '|', '\\', ':', ';', '"', "'", '<', '>', ',', '.', '?', '/']):
                range_name = f"'{actual_sheet_name}'!A1:ZZ1000"
            else:
                range_name = f"{actual_sheet_name}!A1:ZZ1000"
        
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()
        except HttpError as e:
            # If using API key and sheet name fails, try without sheet name (uses first sheet)
            # 如果使用 API key 且 sheet 名称失败，尝试不使用 sheet 名称（使用第一个 sheet）
            if api_key and sheet_name:
                print(f"[WARN] Failed to read sheet '{actual_sheet_name}', trying without sheet name...", file=sys.stderr)
                try:
                    # Try reading without sheet name (will use first sheet)
                    # 尝试不使用 sheet 名称读取（将使用第一个 sheet）
                    result = sheets_service.spreadsheets().values().get(
                        spreadsheetId=spreadsheet_id,
                        range="A1:ZZ1000"
                    ).execute()
                    print(f"[INFO] Successfully read first sheet (without name)", file=sys.stderr)
                    # When reading without sheet name, we can't determine the actual name with API key
                    # 不使用 sheet 名称读取时，使用 API key 无法确定实际的名称
                    # Set to empty string to indicate no sheet name should be used in JavaScript
                    # 设置为空字符串，表示在 JavaScript 中不应使用 sheet 名称
                    actual_sheet_name = ""
                except HttpError as e2:
                    raise Exception(f"Failed to read Google Sheet. Original error: {str(e)}. Tried without sheet name: {str(e2)}")
            else:
                raise
        
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
        return df, actual_sheet_name
        
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


def generate_static_gallery_html(spreadsheet_id: str, sheet_name: str = None, images_dir: Path = None, client_id: str = None, api_key: str = None) -> str:
    """Generate static HTML gallery from Google Sheet data.
    
    Args:
        spreadsheet_id: Google Spreadsheet ID
        sheet_name: Sheet tab name (optional)
        images_dir: Directory containing images
        client_id: OAuth 2.0 Client ID for browser-based feedback submission
        api_key: Google API Key for reading public sheets (optional, if not provided will use OAuth)
    """
    try:
        # Read data from Google Sheet
        # Use API key if provided (for public sheets), otherwise use OAuth
        # 如果提供了 API key 则使用它（用于公开的 Sheet），否则使用 OAuth
        if api_key:
            print(f"[INFO] Using API key to read Google Sheet (public sheet)")
        else:
            print(f"[INFO] Using OAuth to read Google Sheet (private sheet)")
        
        print(f"[INFO] Reading data from Google Sheet: {spreadsheet_id}")
        df, actual_sheet_name = read_sheet_as_dataframe(spreadsheet_id, sheet_name, api_key)
        
        # If actual_sheet_name is empty, it means we read without sheet name (API key case)
        # 如果 actual_sheet_name 为空，表示我们未使用 sheet 名称读取（API key 情况）
        if not actual_sheet_name:
            print(f"[WARN] Using API key and sheet name not specified or invalid. JavaScript will use first sheet without name.", file=sys.stderr)
        
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
        
        # Build HTML with dynamic header (will be updated by JavaScript)
        # 构建带有动态头部的 HTML（将由 JavaScript 更新）
        html_content = build_html("".join(cards_html), title="Foodlog Review Tool")
        
        # Replace the static hint with a placeholder that will be updated by JavaScript
        # 将静态提示替换为将由 JavaScript 更新的占位符
        html_content = html_content.replace(
            '<div class="hint">by Chengyao </div>',
            '<div class="hint" id="user-name-display"></div>'
        )
        
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
const SCOPES = 'https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/userinfo.email';

let tokenClient;
let accessToken = null;
let gapiInitialized = false;

// Token storage keys
// Token 存储键
const TOKEN_STORAGE_KEY = 'google_sheets_access_token';
const TOKEN_EXPIRY_KEY = 'google_sheets_token_expiry';

// Helper function to format range with proper sheet name escaping
// 辅助函数：正确格式化 range，处理 sheet 名称中的特殊字符
function formatRange(range) {{
    // If SHEET_NAME is empty, don't use sheet name (will use first sheet)
    // 如果 SHEET_NAME 为空，不使用 sheet 名称（将使用第一个 sheet）
    if (!SHEET_NAME) {{
        return range;
    }}
    // If sheet name contains spaces or special characters, wrap it in single quotes
    // 如果 sheet 名称包含空格或特殊字符，用单引号包裹
    const needsQuotes = /[\\s!@#$%^&*()+=\\[\\]{{}}\\|\\\\:;"'<>,\\.?/]/.test(SHEET_NAME);
    const sheetPart = needsQuotes ? `'${{SHEET_NAME}}'` : SHEET_NAME;
    return `${{sheetPart}}!${{range}}`;
}}

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
            }}).then(async () => {{
                console.log('[DEBUG] Google API client initialized successfully');
                gapiInitialized = true;
                // If we already have a token, set it now
                // 如果已经有 token，现在设置它
                if (accessToken) {{
                    gapi.client.setToken({{ access_token: accessToken }});
                    // Try to get user email if not already saved (using userinfo endpoint)
                    // 如果尚未保存，尝试获取用户邮箱（使用 userinfo endpoint）
                    if (!getSavedUserEmail()) {{
                        const userEmail = await getUserEmail();
                        if (userEmail) {{
                            console.log('[DEBUG] User email saved on page load:', userEmail);
                        }}
                    }}
                }}
            }}).catch(err => {{
                console.error('[ERROR] Failed to initialize Google API:', err);
                console.error('[ERROR] Error details:', err.message, err.stack);
                // Try to set initialized anyway if it's just a discovery doc issue
                // 如果只是 discovery doc 问题，尝试仍然设置初始化
                gapiInitialized = true;
            }});
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
            range: formatRange('1:1'),
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
            range: formatRange('1:1'),
        }});
        
        const headers = response.result.values[0] || [];
        const newColIdx = headers.length;
        const colLetter = getColumnLetter(newColIdx);
        
        // Add the new column header
        // 添加新列的表头
        await gapi.client.sheets.spreadsheets.values.update({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: formatRange(`${{colLetter}}1`),
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
            range: formatRange('A:ZZ'),
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
                range: formatRange(`${{colLetter}}${{rowIdx}}`),
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
        
        // Get user email for privacy filtering
        // 获取用户邮箱用于隐私过滤
        const userEmail = getSavedUserEmail();
        
        // Add new feedback with email
        // 添加新反馈（包含邮箱）
        feedbackList.push({{
            rd_name: rdName,
            feedback: rdFeedback,
            feedbackedAt: new Date().toISOString(),
            user_email: userEmail  // Add email for privacy filtering
        }});
        console.log('[DEBUG] Adding new feedback, total:', feedbackList.length);
        
        // Write back to sheet
        // 写回 sheet
        console.log('[DEBUG] Writing to sheet...');
        await gapi.client.sheets.spreadsheets.values.update({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: formatRange(`${{colLetter}}${{rowIdx}}`),
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

// Function to refresh feedbacks from Google Sheet
// 从 Google Sheet 刷新反馈
async function refreshFeedbacksFromSheet() {{
    console.log('[DEBUG] Refreshing feedbacks from Google Sheet...');
    
    if (!gapiInitialized) {{
        console.log('[DEBUG] Waiting for Google API initialization...');
        await new Promise((resolve) => {{
            const checkInit = setInterval(() => {{
                if (gapiInitialized) {{
                    clearInterval(checkInit);
                    resolve();
                }}
            }}, 100);
        }});
    }}
    
    // Check if we have access token
    // 检查是否有访问令牌
    if (!accessToken) {{
        loadSavedToken();
    }}
    
    if (!accessToken) {{
        console.log('[DEBUG] No access token available, skipping refresh');
        return;
    }}
    
    try {{
        // Set token for API calls
        // 为 API 调用设置 token
        if (gapiInitialized) {{
            gapi.client.setToken({{ access_token: accessToken }});
        }}
        
        // Read all data from sheet
        // 从 sheet 读取所有数据
        const response = await gapi.client.sheets.spreadsheets.values.get({{
            spreadsheetId: GOOGLE_SHEET_ID,
            range: formatRange('A:ZZ'),
        }});
        
        const values = response.result.values || [];
        if (values.length === 0) {{
            console.log('[DEBUG] Sheet is empty');
            return;
        }}
        
        // Find FoodLogId and RD Feedback columns
        // 找到 FoodLogId 和 RD Feedback 列
        const headers = values[0];
        const foodlogIdCol = headers.indexOf('FoodLogId');
        const rdFeedbackCol = headers.indexOf('RD Feedback');
        
        if (foodlogIdCol === -1) {{
            console.log('[DEBUG] FoodLogId column not found');
            return;
        }}
        
        if (rdFeedbackCol === -1) {{
            console.log('[DEBUG] RD Feedback column not found, no feedbacks to refresh');
            return;
        }}
        
        // Update feedbacks for each card
        // 更新每个卡片的反馈
        for (let i = 1; i < values.length; i++) {{
            const row = values[i];
            if (row.length <= foodlogIdCol) continue;
            
            const foodlogId = row[foodlogIdCol];
            if (!foodlogId) continue;
            
            const reviewDisplay = document.getElementById('review-display-' + foodlogId);
            if (!reviewDisplay) continue;
            
            // Get current feedback value
            // 获取当前反馈值
            let feedbackValue = '';
            if (row.length > rdFeedbackCol) {{
                feedbackValue = row[rdFeedbackCol] || '';
            }}
            
            // Parse and display feedbacks
            // 解析并显示反馈
            let feedbackList = [];
            if (feedbackValue && feedbackValue.trim()) {{
                try {{
                    const feedbackStr = feedbackValue.trim();
                    if (feedbackStr.startsWith('[')) {{
                        feedbackList = JSON.parse(feedbackStr);
                    }} else if (feedbackStr.startsWith('{{')) {{
                        feedbackList = [JSON.parse(feedbackStr)];
                    }}
                }} catch (e) {{
                    console.warn('[WARN] Failed to parse feedback for', foodlogId, ':', e);
                }}
            }}
            
            // Clear existing feedbacks and add all from sheet
            // 清除现有反馈并添加 sheet 中的所有反馈
            reviewDisplay.innerHTML = '';
            
            // Get current user email and name for privacy filtering
            // 获取当前用户邮箱和名字用于隐私过滤
            const currentUserEmail = getSavedUserEmail();
            const currentUserName = getSavedUserName();
            const isAdmin = currentUserName && currentUserName.toLowerCase().trim() === 'admin';
            
            if (feedbackList.length > 0) {{
                feedbackList.forEach(feedback => {{
                    if (typeof feedback === 'object' && feedback !== null) {{
                        // Admin users can see all feedbacks
                        // Admin 用户可以查看所有反馈
                        if (!isAdmin) {{
                            // Privacy filter: only show feedbacks from current user's email
                            // 隐私过滤：只显示当前用户邮箱的反馈
                            const feedbackEmail = feedback.user_email || '';
                            
                            // If current user has email, only show feedbacks with matching email
                            // 如果当前用户有邮箱，只显示匹配邮箱的反馈
                            if (currentUserEmail) {{
                                if (!feedbackEmail || feedbackEmail !== currentUserEmail) {{
                                    // Skip this feedback if it doesn't belong to current user
                                    // 如果反馈不属于当前用户则跳过
                                    return;
                                }}
                            }} else {{
                                // If current user doesn't have email (not logged in), don't show any feedbacks
                                // 如果当前用户没有邮箱（未登录），不显示任何反馈
                                return;
                            }}
                        }}
                        // If isAdmin, continue to show all feedbacks (no filtering)
                        // 如果是 admin，继续显示所有反馈（不过滤）
                        
                        const rdName = feedback.rd_name || 'Unknown';
                        const feedbackText = feedback.feedback || '';
                        const feedbackedAt = feedback.feedbackedAt || '';
                        
                        // Format timestamp
                        // 格式化时间戳
                        let timestamp = feedbackedAt;
                        try {{
                            const dt = new Date(feedbackedAt);
                            if (!isNaN(dt.getTime())) {{
                                timestamp = dt.getFullYear() + '-' + 
                                    String(dt.getMonth() + 1).padStart(2, '0') + '-' + 
                                    String(dt.getDate()).padStart(2, '0') + ' ' + 
                                    String(dt.getHours()).padStart(2, '0') + ':' + 
                                    String(dt.getMinutes()).padStart(2, '0') + ':' + 
                                    String(dt.getSeconds()).padStart(2, '0');
                            }}
                        }} catch (e) {{
                            // Keep original timestamp
                        }}
                        
                        // Escape HTML
                        // 转义 HTML
                        function escapeHtml(text) {{
                            const div = document.createElement('div');
                            div.textContent = text;
                            return div.innerHTML;
                        }}
                        
                        const escapedName = escapeHtml(rdName);
                        
                        // Check if feedback is questionnaire format (JSON)
                        // 检查反馈是否为问卷格式（JSON）
                        let escapedFeedback = '';
                        try {{
                            const questionnaireData = typeof feedbackText === 'string' ? JSON.parse(feedbackText) : feedbackText;
                            if (typeof questionnaireData === 'object' && questionnaireData !== null && ('q1_most_important' in questionnaireData || 'q1_clinically_appropriate' in questionnaireData)) {{
                                // Format questionnaire data
                                // 格式化问卷数据
                                // Support both old and new format for backward compatibility
                                let questions = [];
                                if ('q1_most_important' in questionnaireData) {{
                                    // New format
                                    questions = [
                                        {{text: 'The insight correctly identifies and focuses on the most important thing about this meal', value: questionnaireData.q1_most_important || ''}},
                                        {{text: 'The suggested action (if any) makes sense as a secondary step', value: questionnaireData.q2_action_makes_sense || ''}},
                                        {{text: 'Clinically appropriate, safe, patient-friendly and comfortable sending to a patient', value: questionnaireData.q3_clinically_appropriate || ''}}
                                    ];
                                }} else {{
                                    // Old format (backward compatibility)
                                    questions = [
                                        {{text: 'Clinically appropriate and safe', value: questionnaireData.q1_clinically_appropriate || ''}},
                                        {{text: 'Main message focuses on most important thing', value: questionnaireData.q2_main_message || ''}},
                                        {{text: 'Reasonably reflects what\\'s on plate/log', value: questionnaireData.q3_reflects_plate || ''}},
                                        {{text: 'Suggested action makes sense', value: questionnaireData.q4_action_makes_sense || ''}},
                                        {{text: 'Tone is supportive and patient-friendly', value: questionnaireData.q5_tone || ''}},
                                        {{text: 'Comfortable sending to patients', value: questionnaireData.q6_comfortable_sending || ''}}
                                    ];
                                }}
                                
                                escapedFeedback = '<div class="questionnaire-results">';
                                questions.forEach(q => {{
                                    if (q.value) {{
                                        escapedFeedback += '<div class="question-result"><strong>' + escapeHtml(q.text) + ':</strong> ' + escapeHtml(String(q.value)) + '</div>';
                                    }}
                                }});
                                
                                // Support both old and new format for text fields
                                const whatWorked = questionnaireData.q4_what_worked || questionnaireData.q7_what_worked;
                                const whatFeltOff = questionnaireData.q5_what_felt_off || questionnaireData.q8_what_felt_off;
                                
                                if (whatWorked) {{
                                    escapedFeedback += '<div class="question-result"><strong>What worked well:</strong> ' + escapeHtml(whatWorked).replace(/\\n/g, '<br/>') + '</div>';
                                }}
                                
                                if (whatFeltOff) {{
                                    escapedFeedback += '<div class="question-result"><strong>What felt off or risky:</strong> ' + escapeHtml(whatFeltOff).replace(/\\n/g, '<br/>') + '</div>';
                                }}
                                
                                escapedFeedback += '</div>';
                            }} else {{
                                // Regular text feedback
                                // 常规文本反馈
                                escapedFeedback = escapeHtml(feedbackText).replace(/\\n/g, '<br/>');
                            }}
                        }} catch (e) {{
                            // Not JSON, treat as regular text
                            // 不是 JSON，作为常规文本处理
                            escapedFeedback = escapeHtml(feedbackText).replace(/\\n/g, '<br/>');
                        }}
                        
                        // Create feedback item
                        // 创建反馈项
                        const feedbackItem = document.createElement('div');
                        feedbackItem.className = 'review-display-item';
                        feedbackItem.innerHTML = 
                            '<div class="review-display-header">RD Feedback:</div>' +
                            '<div class="review-display-content">' + escapedFeedback + '</div>' +
                            '<div class="review-display-meta">By: ' + escapedName + ' | ' + timestamp + '</div>';
                        
                        reviewDisplay.appendChild(feedbackItem);
                    }}
                }});
                
                // Make sure display area is visible
                // 确保显示区域可见
                reviewDisplay.style.display = 'block';
            }} else {{
                // Hide if no feedbacks
                // 如果没有反馈则隐藏
                reviewDisplay.style.display = 'none';
            }}
        }}
        
        console.log('[DEBUG] Feedbacks refreshed from Google Sheet');
        
    }} catch (error) {{
        console.error('[ERROR] Failed to refresh feedbacks:', error);
        // Don't show error to user, just log it
        // 不向用户显示错误，只记录
    }}
}}

// Update header with user name
// 用用户名更新头部
function updateHeaderWithUserName(userName) {{
    const hintDiv = document.getElementById('user-name-display') || document.querySelector('.header .hint');
    if (hintDiv && userName) {{
        // Clear existing content
        // 清除现有内容
        hintDiv.innerHTML = '';
        
        // Create text span
        // 创建文本 span
        const textSpan = document.createElement('span');
        textSpan.textContent = 'Reviewer: ' + userName;
        hintDiv.appendChild(textSpan);
        
        // Create logout button
        // 创建登出按钮
        const logoutBtn = document.createElement('button');
        logoutBtn.className = 'logout-btn';
        logoutBtn.textContent = 'Logout';
        logoutBtn.title = 'Sign out / 登出';
        logoutBtn.onclick = handleLogout;
        hintDiv.appendChild(logoutBtn);
    }}
}}

// Handle logout
// 处理登出
function handleLogout() {{
    if (confirm('Are you sure you want to sign out? 确定要登出吗？')) {{
        // Clear all stored data
        // 清除所有存储的数据
        try {{
            if (typeof Storage !== 'undefined' && window.localStorage) {{
                localStorage.removeItem('rd_user_name');
                localStorage.removeItem('rd_user_email');
                localStorage.removeItem(TOKEN_STORAGE_KEY);
                localStorage.removeItem(TOKEN_EXPIRY_KEY);
            }}
        }} catch (e) {{
            console.warn('[WARN] Failed to clear localStorage:', e);
        }}
        
        // Clear gapi token
        // 清除 gapi token
        if (gapiInitialized && gapi.client) {{
            gapi.client.setToken(null);
        }}
        
        // Clear global variables
        // 清除全局变量
        accessToken = null;
        gapiInitialized = false;
        
        // Clear user name display
        // 清除用户名显示
        const hintDiv = document.getElementById('user-name-display');
        if (hintDiv) {{
            hintDiv.textContent = '';
        }}
        
        // Clear all feedback displays
        // 清除所有反馈显示
        const reviewDisplays = document.querySelectorAll('.review-display');
        reviewDisplays.forEach(display => {{
            display.innerHTML = '';
        }});
        
        // Show login overlay
        // 显示登录覆盖层
        showLoginOverlay();
        
        console.log('[DEBUG] User logged out');
    }}
}}

// Show/hide login overlay
// 显示/隐藏登录覆盖层
function showLoginOverlay() {{
    const overlay = document.getElementById('login-overlay');
    if (overlay) {{
        overlay.style.display = 'flex';
    }}
}}

function hideLoginOverlay() {{
    const overlay = document.getElementById('login-overlay');
    if (overlay) {{
        overlay.style.display = 'none';
    }}
}}

// Toggle raw data display
// 切换原始数据显示
function toggleRawData(id) {{
    const content = document.getElementById(id);
    if (!content) {{
        console.error('[ERROR] Content element not found:', id);
        return;
    }}
    const icon = document.getElementById('toggle-icon-' + id);
    if (!icon) {{
        console.error('[ERROR] Icon element not found:', 'toggle-icon-' + id);
        return;
    }}
    if (content.classList.contains('expanded')) {{
        content.classList.remove('expanded');
        icon.classList.add('collapsed');
    }} else {{
        content.classList.add('expanded');
        icon.classList.remove('collapsed');
    }}
}}

// Get saved user name from localStorage
// 从 localStorage 获取保存的用户名
function getSavedUserName() {{
    try {{
        if (typeof Storage !== 'undefined' && window.localStorage) {{
            return localStorage.getItem('rd_user_name') || '';
        }}
    }} catch (e) {{
        console.warn('[WARN] Failed to get saved user name:', e);
    }}
    return '';
}}

// Get saved user email from localStorage
// 从 localStorage 获取保存的用户邮箱
function getSavedUserEmail() {{
    try {{
        if (typeof Storage !== 'undefined' && window.localStorage) {{
            return localStorage.getItem('rd_user_email') || '';
        }}
    }} catch (e) {{
        console.warn('[WARN] Failed to get saved user email:', e);
    }}
    return '';
}}

// Get user email from Google OAuth2 userinfo endpoint
// 从 Google OAuth2 userinfo endpoint 获取用户邮箱
async function getUserEmail() {{
    try {{
        if (!accessToken) {{
            console.warn('[WARN] No access token, cannot get user email');
            return '';
        }}
        
        // Use Google OAuth2 userinfo endpoint (no need for People API)
        // 使用 Google OAuth2 userinfo endpoint（不需要 People API）
        try {{
            const response = await fetch('https://www.googleapis.com/oauth2/v2/userinfo', {{
                headers: {{
                    'Authorization': `Bearer ${{accessToken}}`
                }}
            }});
            
            if (response.ok) {{
                const userInfo = await response.json();
                const email = userInfo.email || '';
                if (email) {{
                    console.log('[DEBUG] Got user email from userinfo endpoint:', email);
                    // Save to localStorage
                    // 保存到 localStorage
                    if (typeof Storage !== 'undefined' && window.localStorage) {{
                        localStorage.setItem('rd_user_email', email);
                    }}
                    return email;
                }}
            }} else {{
                console.warn('[WARN] Failed to get email from userinfo endpoint:', response.status, response.statusText);
            }}
        }} catch (e) {{
            console.warn('[WARN] Failed to get email from userinfo endpoint:', e);
        }}
        
        return '';
    }} catch (e) {{
        console.warn('[WARN] Failed to get user email:', e);
        return '';
    }}
}}

// Handle login button click
// 处理登录按钮点击
function handleLogin() {{
    // Get name from input
    // 从输入框获取名字
    const nameInput = document.getElementById('login-name-input');
    if (!nameInput) {{
        console.error('[ERROR] Name input not found');
        return;
    }}
    
    const userName = nameInput.value.trim();
    if (!userName) {{
        const errorMsg = document.getElementById('login-error');
        if (errorMsg) {{
            errorMsg.textContent = 'Please enter your name';
            errorMsg.style.display = 'block';
        }}
        nameInput.focus();
        return;
    }}
    
    // Save user name to localStorage for later use
    // 保存用户名到 localStorage 供后续使用
    try {{
        if (typeof Storage !== 'undefined' && window.localStorage) {{
            localStorage.setItem('rd_user_name', userName);
            console.log('[DEBUG] Saved user name to localStorage');
        }}
    }} catch (e) {{
        console.warn('[WARN] Failed to save user name:', e);
    }}
    
    if (!tokenClient) {{
        console.error('[ERROR] Token client not initialized');
        const errorMsg = document.getElementById('login-error');
        if (errorMsg) {{
            errorMsg.textContent = 'Authorization service not available. Please refresh the page.';
            errorMsg.style.display = 'block';
        }}
        return;
    }}
    
    // Disable login button
    // 禁用登录按钮
    const loginBtn = document.getElementById('login-btn');
    if (loginBtn) {{
        loginBtn.disabled = true;
        loginBtn.textContent = 'Authorizing...';
    }}
    
    // Hide error message
    // 隐藏错误消息
    const errorMsg = document.getElementById('login-error');
    if (errorMsg) {{
        errorMsg.style.display = 'none';
    }}
    
    console.log('[DEBUG] Login button clicked, requesting authorization...');
    
    tokenClient.callback = async (response) => {{
        // Re-enable login button
        // 重新启用登录按钮
        if (loginBtn) {{
            loginBtn.disabled = false;
            loginBtn.textContent = 'Sign in with Google';
        }}
        
        if (response.error) {{
            console.error('[ERROR] OAuth error:', response.error);
            if (errorMsg) {{
                errorMsg.textContent = 'Authorization failed: ' + response.error;
                errorMsg.style.display = 'block';
            }}
            return;
        }}
        
        console.log('[DEBUG] Access token obtained');
        accessToken = response.access_token;
        // Save token to localStorage
        // 保存 token 到 localStorage
        if (response.expires_in) {{
            saveToken(accessToken, response.expires_in);
        }}
        
        // Update header with user name
        // 用用户名更新头部
        updateHeaderWithUserName(userName);
        
        // Initialize gapi if not already initialized
        // 如果尚未初始化则初始化 gapi
        if (!gapiInitialized && typeof gapi !== 'undefined') {{
            gapi.load('client', () => {{
                gapi.client.init({{
                    discoveryDocs: DISCOVERY_DOCS,
                }}).then(async () => {{
                    gapiInitialized = true;
                    gapi.client.setToken({{ access_token: accessToken }});
                    console.log('[DEBUG] Google API client initialized after login');
                    
                    // Get and save user email (using userinfo endpoint, no People API needed)
                    // 获取并保存用户邮箱（使用 userinfo endpoint，不需要 People API）
                    const userEmail = await getUserEmail();
                    if (userEmail) {{
                        console.log('[DEBUG] User email saved:', userEmail);
                    }}
                    
                    hideLoginOverlay();
                    refreshFeedbacksFromSheet();
                }}).catch(err => {{
                    console.error('[ERROR] Failed to initialize Google API:', err);
                    gapiInitialized = true; // Set anyway to proceed
                    hideLoginOverlay();
                }});
            }});
        }} else {{
            if (gapiInitialized) {{
                gapi.client.setToken({{ access_token: accessToken }});
                // Get and save user email
                // 获取并保存用户邮箱
                getUserEmail().then(email => {{
                    if (email) {{
                        console.log('[DEBUG] User email saved:', email);
                    }}
                }});
            }}
            hideLoginOverlay();
            refreshFeedbacksFromSheet();
        }}
    }};
    
    tokenClient.requestAccessToken({{ prompt: 'select_account' }});
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
        showLoginOverlay();
        return;
    }}
    
    // Initialize Google Identity Services first (for login button)
    // 首先初始化 Google Identity Services（用于登录按钮）
    if (typeof google !== 'undefined' && google.accounts) {{
        tokenClient = google.accounts.oauth2.initTokenClient({{
            client_id: CLIENT_ID,
            scope: SCOPES,
            callback: (response) => {{
                // This callback will be set by handleLogin
                // 这个回调将由 handleLogin 设置
            }},
        }});
        console.log('[DEBUG] Google Identity Services initialized');
    }}
    
    // Try to load saved token
    // 尝试加载保存的 token
    const hasToken = loadSavedToken();
    
    if (!hasToken) {{
        // No token, show login overlay
        // 没有 token，显示登录覆盖层
        console.log('[DEBUG] No saved token, showing login overlay');
        showLoginOverlay();
    }} else {{
        // Has token, initialize API and refresh feedbacks
        // 有 token，初始化 API 并刷新反馈
        console.log('[DEBUG] Found saved token, initializing API...');
        initGoogleAPI();
        
        // Check if Google API libraries are loaded
        // 检查 Google API 库是否已加载
        setTimeout(() => {{
            if (typeof gapi === 'undefined') {{
                console.error('[ERROR] Google API (gapi) not loaded. Check if the script tag is correct.');
            }} else {{
                console.log('[DEBUG] Google API (gapi) loaded');
                // Try to refresh feedbacks after API is loaded
                // 在 API 加载后尝试刷新反馈
                setTimeout(() => {{
                    refreshFeedbacksFromSheet();
                }}, 2000);
            }}
        }}, 1000);
    }}
    
    // Update header with saved user name
    // 用保存的用户名更新头部
    const savedName = getSavedUserName();
    if (savedName) {{
        updateHeaderWithUserName(savedName);
    }}
    
    // Handle form submissions
    // 处理表单提交
    const forms = document.querySelectorAll('.rd-feedback-form');
    forms.forEach(function(form) {{
        form.addEventListener('submit', async function(e) {{
            e.preventDefault();
            
            const foodlogId = form.getAttribute('data-foodlog-id');
            
            // Get saved user name from localStorage
            // 从 localStorage 获取保存的用户名
            const rdName = getSavedUserName();
            
            if (!rdName) {{
                const statusDiv = form.querySelector('.form-status');
                statusDiv.textContent = 'Please sign in first';
                statusDiv.className = 'form-status error';
                showLoginOverlay();
                return;
            }}
            
            // Collect questionnaire data
            // 收集问卷数据
            const questionnaireData = {{
                q1_most_important: form.querySelector('input[name="q1_most_important"]:checked')?.value || '',
                q2_action_makes_sense: form.querySelector('input[name="q2_action_makes_sense"]:checked')?.value || '',
                q3_clinically_appropriate: form.querySelector('input[name="q3_clinically_appropriate"]:checked')?.value || '',
                q4_what_worked: form.querySelector('textarea[name="q4_what_worked"]')?.value.trim() || '',
                q5_what_felt_off: form.querySelector('textarea[name="q5_what_felt_off"]')?.value.trim() || ''
            }};
            
            const submitBtn = form.querySelector('.submit-btn');
            const statusDiv = form.querySelector('.form-status');
            
            // Validate required fields
            // 验证必填字段
            if (!questionnaireData.q1_most_important || 
                !questionnaireData.q2_action_makes_sense || 
                !questionnaireData.q3_clinically_appropriate) {{
                statusDiv.textContent = 'Please answer all required questions';
                statusDiv.className = 'form-status error';
                return;
            }}
            
            // Format feedback as JSON string
            // 将反馈格式化为 JSON 字符串
            const rdFeedback = JSON.stringify(questionnaireData, null, 2);
            
            submitBtn.disabled = true;
            statusDiv.textContent = 'Submitting...';
            statusDiv.className = 'form-status';
            
            try {{
                console.log('[DEBUG] Form submitted, calling addFeedbackViaSheetsAPI...');
                const result = await addFeedbackViaSheetsAPI(foodlogId, rdName, rdFeedback);
                console.log('[DEBUG] Result received:', result);
                const response = {{ ok: result.success, json: async () => result }};
                
                if (response.ok && result.success) {{
                    statusDiv.textContent = 'Success! Feedback saved.';
                    statusDiv.className = 'form-status success';
                    
                    // Clear form
                    // 清空表单
                    form.reset();
                }} else {{
                    statusDiv.textContent = result.error || 'Submission failed';
                    statusDiv.className = 'form-status error';
                }}
            }} catch (error) {{
                console.error('[ERROR] Exception during submission:', error);
                console.error('[ERROR] Error stack:', error.stack);
                statusDiv.textContent = 'Submission failed: ' + (error.message || 'Unknown error');
                statusDiv.className = 'form-status error';
            }} finally {{
                submitBtn.disabled = false;
            }}
        }});
    }});
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
        
        # Remove the old form submission handler from build_html since we're adding our own
        # 从 build_html 中移除旧的表单提交处理程序，因为我们要添加自己的
        # The form submission is now handled in the DOMContentLoaded event listener above
        # 表单提交现在在上面的 DOMContentLoaded 事件监听器中处理
        
        # Remove the original form submission code from build_html
        # 从 build_html 中移除原始的表单提交代码
        old_form_handler_pattern = (
            r"const forms = document\.querySelectorAll\('\.rd-feedback-form'\);\s+"
            r"forms\.forEach\(function\(form\) \{\s+"
            r"form\.addEventListener\('submit', async function\(e\) \{\s+"
            r"e\.preventDefault\(\);\s+"
            r"const foodlogId = form\.getAttribute\('data-foodlog-id'\);\s+"
            r"const rdName = form\.querySelector\('input\[name=\"rd_name\"\]'\)\.value\.trim\(\);\s+"
            r"const rdFeedback = form\.querySelector\('textarea\[name=\"rd_feedback\"\]'\)\.value\.trim\(\);\s+"
            r"const submitBtn = form\.querySelector\('\.submit-btn'\);\s+"
            r"const statusDiv = form\.querySelector\('\.form-status'\);\s+"
            r"if \(!rdName \|\| !rdFeedback\) \{\s+"
            r"statusDiv\.textContent = 'Please fill in RD name and feedback';\s+"
            r"statusDiv\.className = 'form-status error';\s+"
            r"return;\s+"
            r"\}\s+"
            r"submitBtn\.disabled = true;\s+"
            r"statusDiv\.textContent = 'Submitting\.\.\.';\s+"
            r"statusDiv\.className = 'form-status';\s+"
            r"try \{\s+"
            r"const response = await fetch\('/api/add-review',.*?"
            r"const result = await response\.json\(\);\s+"
            r"if \(response\.ok && result\.success\) \{\s+"
            r"statusDiv\.textContent = 'Success! Feedback saved\.';\s+"
            r"statusDiv\.className = 'form-status success';\s+"
            r"// Reload the page.*?"
            r"setTimeout\(function\(\) \{\s+"
            r"window\.location\.reload\(\);\s+"
            r"\}, 500\);\s+"
            r"\} else \{\s+"
            r"statusDiv\.textContent = result\.error \|\| 'Submission failed';\s+"
            r"statusDiv\.className = 'form-status error';\s+"
            r"\}\s+"
            r"\} catch \(error\) \{\s+"
            r"statusDiv\.textContent = 'Submission failed: ' \+ error\.message;\s+"
            r"statusDiv\.className = 'form-status error';\s+"
            r"\} finally \{\s+"
            r"submitBtn\.disabled = false;\s+"
            r"\}\s+"
            r"\}\);\s+"
            r"\}\);\s+"
            r"\}\);\s+"
        )
        
        html_content = re.sub(old_form_handler_pattern, '', html_content, flags=re.DOTALL)
        
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
        
        
        # Add login overlay HTML and CSS
        # 添加登录覆盖层 HTML 和 CSS
        login_overlay_html = """
<style>
.login-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 10000;
}
.login-box {
    background: var(--card);
    padding: 32px;
    border-radius: 12px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    max-width: 400px;
    width: 90%;
    text-align: left;
}
.login-box h2 {
    margin: 0 0 16px 0;
    font-size: 20px;
    color: var(--text);
    text-align: center;
}
.login-box p {
    margin: 0 0 24px 0;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.5;
    text-align: center;
}
.login-form-group {
    margin-bottom: 20px;
}
.login-form-group label {
    display: block;
    font-size: 13px;
    color: var(--text);
    margin-bottom: 6px;
    font-weight: 500;
}
.login-form-group input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 14px;
    font-family: inherit;
    background: var(--card);
    color: var(--text);
    box-sizing: border-box;
}
.login-form-group input:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(59,130,246,0.1);
}
.login-form-group {
    margin-bottom: 20px;
    text-align: left;
}
.login-form-group label {
    display: block;
    font-size: 13px;
    color: var(--text);
    margin-bottom: 6px;
    font-weight: 500;
}
.login-form-group input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 14px;
    font-family: inherit;
    background: var(--card);
    color: var(--text);
    box-sizing: border-box;
}
.login-form-group input:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(59,130,246,0.1);
}
.login-btn {
    padding: 12px 24px;
    background: var(--accent);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.2s;
    width: 100%;
    margin-top: 8px;
}
.login-btn:hover {
    background: #2563eb;
}
.login-btn:disabled {
    background: var(--muted);
    cursor: not-allowed;
}
.login-error {
    margin-top: 12px;
    padding: 8px;
    background: #fee2e2;
    border: 1px solid #fecaca;
    border-radius: 6px;
    color: #dc2626;
    font-size: 13px;
    display: none;
}
.login-hint {
    font-size: 12px;
    color: var(--muted);
    margin-top: 16px;
    text-align: center;
    line-height: 1.5;
}
.logout-btn {
    margin-left: 8px;
    padding: 4px 12px;
    background: #ef4444;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.2s;
}
.logout-btn:hover {
    background: #dc2626;
}
.logout-btn:active {
    background: #b91c1c;
}
#user-name-display {
    display: flex;
    align-items: center;
    gap: 4px;
}
</style>
<div id="login-overlay" class="login-overlay" style="display: none;">
    <div class="login-box">
        <h2>Google Sheets Authorization</h2>
        <p>Please enter your name and authorize access to Google Sheets to submit and view feedback.</p>
        <div class="login-form-group">
            <label for="login-name-input">Your Name:</label>
            <input type="text" id="login-name-input" placeholder="Enter your name" required />
        </div>
        <button id="login-btn" class="login-btn" onclick="handleLogin()">Sign in with Google</button>
        <div id="login-error" class="login-error"></div>
        <div class="login-hint">Your access token will be saved in your browser. You won't need to sign in again on future visits.</div>
    </div>
</div>
"""
        
        # Remove RD Name input field from all cards
        # 从所有卡片中移除 RD Name 输入框
        import re
        # Pattern to match the RD Name form group
        # 匹配 RD Name 表单组的模式
        rd_name_pattern = r'<div class="form-group">\s*<label[^>]*>RD Name:</label>\s*<input[^>]*name="rd_name"[^>]*/>\s*</div>\s*'
        html_content = re.sub(rd_name_pattern, '', html_content, flags=re.MULTILINE | re.DOTALL)
        
        # Also try a more flexible pattern
        # 也尝试更灵活的模式
        rd_name_pattern2 = r'<div class="form-group">.*?<label[^>]*>RD Name:</label>.*?<input[^>]*name="rd_name"[^>]*/>.*?</div>'
        html_content = re.sub(rd_name_pattern2, '', html_content, flags=re.MULTILINE | re.DOTALL)
        
        # Replace form submission to use saved name from localStorage
        # 替换表单提交以使用 localStorage 中保存的名字
        # First, replace the rdName assignment
        # 首先，替换 rdName 赋值
        old_form_submit_pattern = (
            r"const rdName = form\.querySelector\('input\[name=\"rd_name\"\]'\)\.value\.trim\(\);"
        )
        new_form_submit_code = """// Get saved name from localStorage
                // 从 localStorage 获取保存的名字
                const rdName = getSavedUserName();
                if (!rdName) {{
                    statusDiv.textContent = 'Please sign in first';
                    statusDiv.className = 'form-status error';
                    showLoginOverlay();
                    return;
                }}"""
        
        html_content = re.sub(old_form_submit_pattern, new_form_submit_code, html_content)
        
        # Also handle the case where rdName might be defined differently
        # 同时处理 rdName 可能以不同方式定义的情况
        html_content = html_content.replace(
            "const rdName = form.querySelector('input[name=\"rd_name\"]').value.trim();",
            """// Get saved name from localStorage
                const rdName = getSavedUserName();
                if (!rdName) {
                    statusDiv.textContent = 'Please sign in first';
                    statusDiv.className = 'form-status error';
                    showLoginOverlay();
                    return;
                }"""
        )
        
        # Also update the validation check
        # 同时更新验证检查
        html_content = html_content.replace(
            "if (!rdName || !rdFeedback) {",
            "if (!rdFeedback) {"
        )
        html_content = html_content.replace(
            "statusDiv.textContent = 'Please fill in RD name and feedback';",
            "statusDiv.textContent = 'Please enter your feedback';"
        )
        
        # Update form clearing to not clear name (since it doesn't exist)
        # 更新表单清空逻辑，不清空名字（因为不存在）
        html_content = html_content.replace(
            "form.querySelector('input[name=\"rd_name\"]').value = '';",
            ""
        )
        
        # Insert login overlay before the closing body tag
        # 在 body 标签结束前插入登录覆盖层
        html_content = html_content.replace(
            '</body>',
            login_overlay_html + '</body>'
        )
        
        # Add code to pre-fill name inputs if saved name exists (for backward compatibility)
        # 如果存在保存的名字，添加代码预填充名字输入框（向后兼容）
        prefill_name_script = """
// Pre-fill RD name inputs with saved name (if they exist)
// 用保存的名字预填充 RD name 输入框（如果存在）
document.addEventListener('DOMContentLoaded', function() {
    const savedName = getSavedUserName();
    if (savedName) {
        const nameInputs = document.querySelectorAll('input[name="rd_name"]');
        nameInputs.forEach(input => {
            input.value = savedName;
        });
    }
});
"""
        html_content = html_content.replace(
            '</script>',
            prefill_name_script + '</script>',
            1  # Only replace the first occurrence (the main script tag)
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
        '--api-key',
        default=None,
        help='Google API Key for reading public sheets (optional, if not provided will use OAuth)'
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
    
    # Get API key from argument or environment variable
    # 从参数或环境变量获取 API key
    api_key = args.api_key or os.environ.get('GOOGLE_SHEETS_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    
    # Check if API key is provided, otherwise require credentials.json for OAuth
    # 检查是否提供了 API key，否则需要 credentials.json 用于 OAuth
    if not api_key:
        if not os.path.exists(CREDENTIALS_FILE):
            print(f"[ERROR] {CREDENTIALS_FILE} not found!", file=sys.stderr)
            print(f"Please either:", file=sys.stderr)
            print(f"  1. Provide --api-key for reading public sheets, or", file=sys.stderr)
            print(f"  2. Download credentials.json from Google Cloud Console for OAuth", file=sys.stderr)
            sys.exit(1)
    
    print(f"[INFO] Google Spreadsheet ID: {spreadsheet_id}")
    print(f"[INFO] Sheet name: {sheet_name}")
    print(f"[INFO] Images directory: {images_dir.resolve()}")
    if api_key:
        print(f"[INFO] Using API key for reading (public sheet)")
    else:
        print(f"[INFO] Using OAuth for reading (private sheet)")
    
    # Generate HTML
    # 生成 HTML
    html_content = generate_static_gallery_html(
        spreadsheet_id,
        sheet_name,
        images_dir,
        args.client_id,
        api_key
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
