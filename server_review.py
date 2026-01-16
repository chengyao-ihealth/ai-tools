#!/usr/bin/env python3
"""
Flask Server for RD Feedback Submission with Dynamic HTML Generation
处理RD Feedback提交的Flask服务器，支持动态HTML生成

This server provides:
1. API endpoint to add RD feedback to the CSV file
2. Dynamic HTML gallery generation from CSV data (real-time updates)
3. Static HTML file serving (backward compatibility)

此服务器提供：
1. API端点来添加RD feedback到CSV文件
2. 从CSV数据动态生成HTML画廊（实时更新）
3. 静态HTML文件服务（向后兼容）
"""
import argparse
import base64
import json
import re
import sys
import html as html_module
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Tuple

import pandas as pd
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Enable CORS for local development

# Global variables for CSV, HTML paths, and images directory
csv_path = None
html_dir = None
images_dir = None

# ============================================================================
# Gallery generation functions (from show_foodlog_gallery.py)
# ============================================================================

def looks_like_json(s: str) -> bool:
    """Check if a string looks like JSON."""
    s = s.strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))


def humanize_json(obj: Any, *, indent_level: int = 0) -> str:
    """Convert dict/list/primitive types to natural language string."""
    indent = "  " * indent_level
    if obj is None:
        return ""
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            k_str = str(k).strip()
            v_str = humanize_json(v, indent_level=indent_level + 1)
            if "\n" in v_str:
                lines.append(f"{indent}{k_str}：\n{v_str}")
            else:
                lines.append(f"{indent}{k_str}：{v_str}")
        return "\n".join(lines)
    if isinstance(obj, (list, tuple, set)):
        if not obj:
            return ""
        if any(isinstance(x, dict) for x in obj):
            out_lines = []
            for i, x in enumerate(obj, 1):
                x_str = humanize_json(x, indent_level=indent_level + 1)
                prefix = f"{indent}- 项{i}: "
                if "\n" in x_str:
                    out_lines.append(prefix + "\n" + x_str)
                else:
                    out_lines.append(prefix + x_str)
            return "\n".join(out_lines)
        return "，".join(str(x) for x in obj)
    return str(obj)


def format_rd_comments(s: Any) -> str:
    """Format RD Comments to show only 'text' and 'commentedAt' fields."""
    if s is None:
        return ""
    obj = s
    if isinstance(s, str):
        s_strip = s.strip()
        if not s_strip:
            return ""
        if looks_like_json(s_strip):
            try:
                obj = json.loads(s_strip)
            except Exception:
                return s_strip
        else:
            return s_strip
    if isinstance(obj, list):
        if not obj:
            return ""
        result_lines = []
        for comment in obj:
            if isinstance(comment, dict):
                text = comment.get("text", "")
                commented_at = comment.get("commentedAt", "")
                lines = []
                if text:
                    lines.append(f"Comment: {text}")
                if commented_at:
                    lines.append(f"Time: {commented_at}")
                if lines:
                    result_lines.append("\n".join(lines))
        return "\n\n".join(result_lines)
    if isinstance(obj, dict):
        text = obj.get("text", "")
        commented_at = obj.get("commentedAt", "")
        lines = []
        if text:
            lines.append(f"Comment: {text}")
        if commented_at:
            lines.append(f"Time: {commented_at}")
        return "\n".join(lines)
    return str(obj)


def format_ingredients(s: Any) -> str:
    """Format Ingredients with structured display format."""
    if s is None:
        return ""
    obj = s
    if isinstance(s, str):
        s_strip = s.strip()
        if not s_strip:
            return ""
        if looks_like_json(s_strip):
            try:
                obj = json.loads(s_strip)
            except Exception:
                return s_strip
        else:
            return s_strip
    if isinstance(obj, list):
        if not obj:
            return ""
        result_lines = []
        for idx, ingredient in enumerate(obj, 1):
            if isinstance(ingredient, dict):
                ingredient_lines = []
                name = ingredient.get("name", "")
                if name:
                    ingredient_lines.append(f"<strong>{idx}. {name}</strong>")
                else:
                    ingredient_lines.append(f"<strong>{idx}. Unknown ingredient</strong>")
                portion = ingredient.get("estimatedPortion", ingredient.get("Portion", ""))
                if portion:
                    ingredient_lines.append(f"Estimated Portion: {portion}")
                nutrition_data = ingredient.get("nutrition", [])
                if nutrition_data:
                    ingredient_lines.append("Nutrition:")
                    if isinstance(nutrition_data, list):
                        for nutr_item in nutrition_data:
                            if isinstance(nutr_item, dict):
                                nutr_name = nutr_item.get("nutrition", "")
                                gram = nutr_item.get("gram", "")
                                if nutr_name:
                                    ingredient_lines.append(f"  - {nutr_name}: {gram}g")
                kcal = ingredient.get("kcalPer100g", "")
                if kcal:
                    ingredient_lines.append(f"kcalPer100g: {kcal}")
                result_lines.append("\n".join(ingredient_lines))
        return "\n\n".join(result_lines)
    if isinstance(obj, dict):
        lines = []
        name = obj.get("name", "")
        if name:
            lines.append(f"<strong>1. {name}</strong>")
        portion = obj.get("estimatedPortion", obj.get("Portion", ""))
        if portion:
            lines.append(f"Estimated Portion: {portion}")
        nutrition_data = obj.get("nutrition", [])
        if nutrition_data:
            lines.append("Nutrition:")
            if isinstance(nutrition_data, list):
                for nutr_item in nutrition_data:
                    if isinstance(nutr_item, dict):
                        nutr_name = nutr_item.get("nutrition", "")
                        gram = nutr_item.get("gram", "")
                        if nutr_name:
                            lines.append(f"  - {nutr_name}: {gram}g")
        kcal = obj.get("kcalPer100g", "")
        if kcal:
            lines.append(f"kcalPer100g: {kcal}")
        return "\n".join(lines)
    return str(obj)


def format_field_value(value: Any, field_name: str) -> str:
    """Format a field value based on its content and field name."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return ""
    if field_name.lower() in ['rd comments', 'rd_comments']:
        return format_rd_comments(value)
    elif field_name.lower() in ['ingredients']:
        return format_ingredients(value)
    elif field_name.lower() in ['aiinsight', 'insight']:
        return value
    elif field_name.lower() in ['aititle', 'mealtitle']:
        return value
    elif field_name.lower() in ['description']:
        return value
    # Special handling for AI Identify Raw Data - don't use humanize_json to avoid adding indentation
    # 特殊处理AI Identify Raw Data - 不使用humanize_json以避免添加缩进
    elif field_name.lower() in ['aiidentifyrawdata', 'ai_identify_raw_data'] or ('identify' in field_name.lower() and 'raw' in field_name.lower()):
        return value  # Return as-is without JSON formatting
    if looks_like_json(value):
        try:
            obj = json.loads(value)
            return humanize_json(obj)
        except Exception:
            return value
    return value


def read_image_as_data_uri(img_path: Path) -> str:
    """Convert local image to data URI."""
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
        return f"data:{mime};base64,{b64}"
    except Exception:
        return ""


def get_display_columns(df: pd.DataFrame) -> List[str]:
    """Get the list of columns to display, excluding system columns and RD Feedback."""
    system_columns = {'MemberId', 'FoodLogId', 'RD Feedback'}
    return [col for col in df.columns if col not in system_columns]


def format_field_name(field_name: str) -> str:
    """Format field name from camelCase/PascalCase to readable format.
    将字段名从驼峰命名转换为可读格式。
    
    Examples:
        AiInsight -> AI Insight
        AiDetectedFoods -> AI Detected Foods
        MealTitle -> Meal Title
    """
    # Insert space before capital letters (except the first one)
    # 在大写字母前插入空格（第一个除外）
    formatted = re.sub(r'(?<!^)(?=[A-Z])', ' ', field_name)
    # Handle "Ai" prefix specially
    # 特殊处理"Ai"前缀
    formatted = formatted.replace('Ai ', 'AI ', 1)
    return formatted


def _build_collapsible_raw_data(raw_data_info: dict, foodlog_id: str) -> str:
    """Build collapsible HTML for AI Identify Raw Data field.
    为AI Identify Raw Data字段构建可折叠的HTML。
    """
    if not raw_data_info:
        return ""
    
    label = raw_data_info['label']
    value = raw_data_info['value']
    allow_html = raw_data_info.get('allow_html', False)
    
    # Escape HTML if needed
    # 如果需要则转义HTML
    if allow_html:
        safe_value = value.replace("\n", "<br/>")
    else:
        safe_value = html_module.escape(value).replace("\n", "<br/>")
    
    unique_id = f"raw-data-{html_module.escape(foodlog_id)}"
    
    return f"""
    <div class="ai-raw-data-container">
        <button type="button" class="ai-raw-data-toggle" onclick="toggleRawData('{unique_id}')">
            <span class="toggle-icon collapsed" id="toggle-icon-{unique_id}">▼</span>
            {html_module.escape(label)}
        </button>
        <div class="ai-raw-data-content" id="{unique_id}">
            <div class="ai-raw-data-scroll">
                {safe_value}
            </div>
        </div>
    </div>
    """


def build_card_html(row, images_dir: Path, display_columns: List[str], row_idx: Any = None) -> str:
    """Build HTML card for a single food log entry with dynamic columns."""
    # Get foodlog_id first (needed for various parts of the card)
    # 首先获取foodlog_id（卡片多个部分需要）
    foodlog_id = ""
    if "FoodLogId" in row.index:
        foodlog_id_val = row["FoodLogId"]
        foodlog_id = str(foodlog_id_val) if pd.notna(foodlog_id_val) else ""
    if not foodlog_id and row_idx is not None:
        foodlog_id = str(row_idx)
    
    raw_imgnames = str(row.get("ImgName", "") or "").strip()
    img_names: Iterable[str] = [x.strip() for x in raw_imgnames.split(";") if x.strip()] if raw_imgnames else []
    
    img_tags = []
    if img_names:
        for name in img_names:
            img_path = images_dir / name
            data_uri = read_image_as_data_uri(img_path)
            if data_uri:
                img_tags.append(f'<img src="{data_uri}" alt="{html_module.escape(name)}" />')
            else:
                img_tags.append(f'<div class="img-missing">缺失：{html_module.escape(name)}</div>')
    else:
        img_tags.append('<div class="img-missing">未提供图片文件名</div>')
    
    def para(label: str, text: str, escape_html: bool = True) -> str:
        if not text:
            return ""
        if escape_html:
            safe = html_module.escape(text).replace("\n", "<br/>")
        else:
            safe = text.replace("\n", "<br/>")
        return f'<div class="field"><div class="label">{html_module.escape(label)}</div><div class="value">{safe}</div></div>'
    
    # Separate AI fields from other fields
    # 分离AI字段和其他字段
    ai_raw_data_field = None
    other_fields = []
    
    # Fields that should be in AI Generated Content (at the end)
    # 应该放在AI Generated Content中的字段（靠后位置）
    ai_content_fields = {'FoodLogLabels', 'MicroAction', 'ActionFamily', 'BestAnchor'}
    
    for col in display_columns:
        if col == "ImgName":
            continue
        value = row[col] if col in row.index else ""
        formatted_value = format_field_value(value, col)
        if not formatted_value:
            continue
        
        # Check if field starts with "Ai" (case-insensitive)
        # 检查字段是否以"Ai"开头（不区分大小写）
        if col.startswith("Ai") or col.startswith("ai"):
            formatted_label = format_field_name(col)
            allow_html = col.lower() in ['ingredients']
            
            # Special handling for AI Identify Raw Data - make it collapsible
            # 特殊处理AI Identify Raw Data - 使其可折叠
            if col.lower() in ['aiidentifyrawdata', 'ai_identify_raw_data'] or 'identify' in col.lower() and 'raw' in col.lower():
                ai_raw_data_field = {
                    'label': formatted_label,
                    'value': formatted_value,
                    'allow_html': allow_html,
                    'foodlog_id': foodlog_id
                }
            else:
                # Add AI fields directly to other_fields (no blue box)
                # 直接将AI字段添加到other_fields（不使用蓝框）
                other_fields.append(para(formatted_label, formatted_value, escape_html=not allow_html))
        elif col in ai_content_fields:
            # Add these fields directly to other_fields (no blue box)
            # 直接将字段添加到other_fields（不使用蓝框）
            allow_html = col.lower() in ['ingredients']
            other_fields.append(para(col, formatted_value, escape_html=not allow_html))
        else:
            allow_html = col.lower() in ['ingredients']
            other_fields.append(para(col, formatted_value, escape_html=not allow_html))
    
    # Add AI Identify Raw Data as collapsible field (if exists)
    # 添加AI Identify Raw Data作为可折叠字段（如果存在）
    if ai_raw_data_field:
        other_fields.append(_build_collapsible_raw_data(ai_raw_data_field, foodlog_id))
    
    # Combine other fields
    # 组合其他字段
    field_html = other_fields
    
    # Check if there are existing feedbacks to display
    existing_feedbacks_html = ""
    if "RD Feedback" in row.index:
        rd_feedback_value = row["RD Feedback"]
        if pd.notna(rd_feedback_value) and str(rd_feedback_value).strip():
            try:
                feedback_str = str(rd_feedback_value).strip()
                if feedback_str.startswith('['):
                    feedback_list = json.loads(feedback_str)
                elif feedback_str.startswith('{'):
                    feedback_list = [json.loads(feedback_str)]
                else:
                    feedback_list = []
                
                # Generate HTML for each feedback
                feedback_items = []
                for feedback in feedback_list:
                    if isinstance(feedback, dict):
                        rd_name = feedback.get("rd_name", "Unknown")
                        feedback_text = feedback.get("feedback", "")
                        feedbacked_at = feedback.get("feedbackedAt", "")
                        
                        # Format timestamp
                        try:
                            dt = datetime.fromisoformat(feedbacked_at.replace('Z', '+00:00'))
                            timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
                        except:
                            timestamp = feedbacked_at
                        
                        escaped_name = html_module.escape(rd_name)
                        escaped_feedback = html_module.escape(feedback_text).replace("\n", "<br/>")
                        
                        feedback_items.append(
                            f'<div class="review-display-item">'
                            f'<div class="review-display-header">RD Feedback:</div>'
                            f'<div class="review-display-content">{escaped_feedback}</div>'
                            f'<div class="review-display-meta">By: {escaped_name} | {timestamp}</div>'
                            f'</div>'
                        )
                
                if feedback_items:
                    existing_feedbacks_html = ''.join(feedback_items)
            except (json.JSONDecodeError, ValueError, Exception):
                # If parsing fails, don't show anything
                pass
    
    review_form = f"""
        <div class="review-form">
            <div class="review-form-title">Add RD Feedback</div>
            <div class="review-form-hint">For labeling the quality of AI-generated content</div>
            <form class="rd-feedback-form" data-foodlog-id="{html_module.escape(foodlog_id)}">
                <div class="form-group">
                    <label for="rd-name-{html_module.escape(foodlog_id)}">RD Name:</label>
                    <input type="text" id="rd-name-{html_module.escape(foodlog_id)}" name="rd_name" class="form-input" required />
                </div>
                <div class="form-group">
                    <label for="rd-feedback-{html_module.escape(foodlog_id)}">Feedback (AI Content Quality Assessment):</label>
                    <textarea id="rd-feedback-{html_module.escape(foodlog_id)}" name="rd_feedback" class="form-textarea" rows="3" placeholder="Please assess the quality of AI-generated content..." required></textarea>
                </div>
                <button type="submit" class="submit-btn">Submit</button>
                <div class="form-status"></div>
            </form>
            <div class="review-display" id="review-display-{html_module.escape(foodlog_id)}">{existing_feedbacks_html}</div>
        </div>
    """
    
    return f"""
    <div class="card" data-foodlog-id="{html_module.escape(foodlog_id)}">
        <div class="images">
            {''.join(img_tags)}
        </div>
        <div class="meta">
            {''.join(field_html)}
        </div>
        {review_form}
    </div>
    """


def build_html(doc_cards: str, title: str = "FoodLog Gallery") -> str:
    """Build complete HTML document with card grid layout."""
    # Get the full HTML template from show_foodlog_gallery.py
    # For brevity, I'll include the essential parts
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html_module.escape(title)}</title>
<style>
:root {{
  --bg: #faf8f5;
  --card: #ffffff;
  --text: #1a1a1a;
  --muted: #6b7280;
  --accent: #3b82f6;
  --border: #e5e7eb;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 24px;
  background: var(--bg); color: var(--text);
  font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Inter,Helvetica,Arial,'Noto Sans','PingFang SC','Microsoft Yahei',sans-serif;
}}
h1 {{
  font-size: 22px; font-weight: 700; margin: 0 0 16px 0;
}}
.header {{
  display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 12px;
}}
.hint {{
  color: var(--muted); font-size: 13px;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 16px;
}}
.card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.images {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
}}
.card img {{
  width: 100%;
  height: auto;
  border-radius: 10px;
  border: 1px solid var(--border);
}}
.img-missing {{
  height: 160px;
  display: grid; place-items: center;
  border-radius: 10px;
  border: 1px dashed var(--border);
  color: var(--muted);
  font-size: 13px;
}}
.meta {{
  display: flex; flex-direction: column; gap: 6px;
}}
.ai-content-box {{
  margin-top: 12px;
  padding: 14px;
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-radius: 8px;
  border-left: 4px solid var(--accent);
  overflow: hidden;
  word-wrap: break-word;
  overflow-wrap: break-word;
  max-width: 100%;
  box-sizing: border-box;
}}
.ai-content-header {{
  font-size: 13px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  word-wrap: break-word;
  overflow-wrap: break-word;
}}
.ai-content-body {{
  display: flex; flex-direction: column; gap: 8px;
  word-wrap: break-word;
  overflow-wrap: break-word;
  max-width: 100%;
  box-sizing: border-box;
}}
.ai-raw-data-container {{
  margin-top: 4px;
}}
.ai-raw-data-toggle {{
  width: 100%;
  padding: 8px 10px;
  background: #e0f2fe;
  border: 1px solid #bae6fd;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 6px;
  transition: background 0.2s;
}}
.ai-raw-data-toggle:hover {{
  background: #bae6fd;
}}
.toggle-icon {{
  font-size: 10px;
  transition: transform 0.2s;
  display: inline-block;
}}
.toggle-icon.collapsed {{
  transform: rotate(-90deg);
}}
.toggle-icon:not(.collapsed) {{
  transform: rotate(0deg);
}}
.ai-raw-data-content {{
  max-height: 0;
  overflow: hidden;
  transition: max-height 0.3s ease-out;
  margin-top: 0;
  opacity: 0;
}}
.ai-raw-data-content.expanded {{
  max-height: 2000px;
  margin-top: 8px;
  opacity: 1;
}}
.ai-raw-data-scroll {{
  margin-top: 0;
  padding: 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text);
  max-height: 400px;
  overflow-y: auto;
  overflow-x: hidden;
  white-space: pre-wrap;
  word-wrap: break-word;
}}
.ai-raw-data-scroll::-webkit-scrollbar {{
  width: 6px;
}}
.ai-raw-data-scroll::-webkit-scrollbar-track {{
  background: #f1f5f9;
  border-radius: 3px;
}}
.ai-raw-data-scroll::-webkit-scrollbar-thumb {{
  background: #cbd5e1;
  border-radius: 3px;
}}
.ai-raw-data-scroll::-webkit-scrollbar-thumb:hover {{
  background: #94a3b8;
}}
.field {{
  max-width: 100%;
  box-sizing: border-box;
  word-wrap: break-word;
  overflow-wrap: break-word;
}}
.field .label {{
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 2px;
  word-wrap: break-word;
  overflow-wrap: break-word;
}}
.field .value {{
  font-size: 14px; line-height: 1.5;
  white-space: normal;
  word-wrap: break-word;
  overflow-wrap: break-word;
  max-width: 100%;
  box-sizing: border-box;
}}
.footer {{
  margin-top: 18px; color: var(--muted); font-size: 12px;
}}
.review-form {{
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}}
.review-form-title {{
  font-size: 14px; font-weight: 600; margin-bottom: 6px; color: var(--text);
}}
.review-form-hint {{
  font-size: 11px; color: var(--muted); margin-bottom: 10px; font-style: italic;
}}
.form-group {{
  margin-bottom: 10px;
}}
.form-group label {{
  display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px;
}}
.form-input, .form-textarea {{
  width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px;
  font-size: 13px; font-family: inherit; background: var(--card); color: var(--text);
}}
.form-input:focus, .form-textarea:focus {{
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(59,130,246,0.1);
}}
.form-textarea {{
  resize: vertical; min-height: 60px;
}}
.submit-btn {{
  padding: 8px 16px; background: var(--accent); color: white; border: none;
  border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer;
  transition: background 0.2s;
}}
.submit-btn:hover {{
  background: #2563eb;
}}
.submit-btn:disabled {{
  background: var(--muted); cursor: not-allowed;
}}
.form-status {{
  margin-top: 8px; font-size: 12px; min-height: 16px;
}}
.form-status.success {{
  color: #10b981;
}}
.form-status.error {{
  color: #ef4444;
}}
.review-display {{
  margin-top: 12px; padding: 10px; background: #f0f9ff; border: 1px solid #bae6fd;
  border-radius: 6px;
}}
.review-display:empty {{
  display: none;
}}
.review-display-item {{
  margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #bae6fd;
}}
.review-display-item:last-child {{
  margin-bottom: 0; padding-bottom: 0; border-bottom: none;
}}
.review-display-header {{
  font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text);
}}
.review-display-content {{
  font-size: 13px; line-height: 1.5; color: var(--text);
}}
.review-display-meta {{
  font-size: 11px; color: var(--muted); margin-top: 6px;
}}
</style>
</head>
<body>
  <div class="header">
    <h1>{html_module.escape(title)}</h1>
    <div class="hint">by Chengyao </div>
  </div>
  <div class="grid">
  {doc_cards}
  </div>
  <div class="footer">Tip: You can use browser search (⌘/Ctrl+F) to quickly locate content by field if there are many images.</div>
<script>
function escapeHtml(text) {{
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}}

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

document.addEventListener('DOMContentLoaded', function() {{
    const forms = document.querySelectorAll('.rd-feedback-form');
    
    forms.forEach(function(form) {{
        form.addEventListener('submit', async function(e) {{
            e.preventDefault();
            
            const foodlogId = form.getAttribute('data-foodlog-id');
            const rdName = form.querySelector('input[name="rd_name"]').value.trim();
            const rdFeedback = form.querySelector('textarea[name="rd_feedback"]').value.trim();
            const submitBtn = form.querySelector('.submit-btn');
            const statusDiv = form.querySelector('.form-status');
            
            if (!rdName || !rdFeedback) {{
                statusDiv.textContent = 'Please fill in RD name and feedback';
                statusDiv.className = 'form-status error';
                return;
            }}
            
            submitBtn.disabled = true;
            statusDiv.textContent = 'Submitting...';
            statusDiv.className = 'form-status';
            
            try {{
                const response = await fetch('/api/add-review', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                    }},
                    body: JSON.stringify({{
                        foodlog_id: foodlogId,
                        rd_name: rdName,
                        rd_feedback: rdFeedback
                    }})
                }});
                
                const result = await response.json();
                
                if (response.ok && result.success) {{
                    statusDiv.textContent = 'Success! Feedback saved.';
                    statusDiv.className = 'form-status success';
                    
                    // Reload the page to show all feedbacks (including the new one)
                    // 重新加载页面以显示所有feedbacks（包括新添加的）
                    setTimeout(function() {{
                        window.location.reload();
                    }}, 500);
                }} else {{
                    statusDiv.textContent = result.error || 'Submission failed';
                    statusDiv.className = 'form-status error';
                }}
            }} catch (error) {{
                statusDiv.textContent = 'Submission failed: ' + error.message;
                statusDiv.className = 'form-status error';
            }} finally {{
                submitBtn.disabled = false;
            }}
        }});
    }});
}});
</script>
</body>
</html>
"""


def generate_gallery_html() -> str:
    """Generate HTML gallery from current CSV data."""
    global csv_path, images_dir
    
    try:
        df = pd.read_csv(csv_path)
        
        if "ImgName" not in df.columns:
            return f"<html><body><h1>Error</h1><p>CSV file does not have ImgName column</p></body></html>"
        
        display_columns = get_display_columns(df)
        cards_html = []
        
        for idx, row in df.iterrows():
            try:
                cards_html.append(build_card_html(row, images_dir, display_columns, row_idx=idx))
            except Exception as e:
                continue
        
        return build_html("".join(cards_html), title="FoodLog Gallery - RD Feedback")
    except Exception as e:
        return f"<html><body><h1>Error</h1><p>Failed to generate gallery: {str(e)}</p></body></html>"


# ============================================================================
# Flask Routes
# ============================================================================

def add_review_to_csv(foodlog_id: str, rd_name: str, rd_feedback: str) -> Tuple[bool, str]:
    """Add feedback to the CSV file in a new "RD Feedback" column (appends to list)."""
    global csv_path
    
    try:
        df = pd.read_csv(csv_path)
        
        if 'FoodLogId' not in df.columns:
            return False, "CSV file does not have FoodLogId column"
        
        mask = df['FoodLogId'] == foodlog_id
        matching_rows = df[mask]
        
        if len(matching_rows) == 0:
            return False, f"FoodLogId not found: {foodlog_id}"
        
        row_idx = matching_rows.index[0]
        
        new_feedback = {
            "rd_name": rd_name,
            "feedback": rd_feedback,
            "feedbackedAt": datetime.now().isoformat()
        }
        
        if 'RD Feedback' not in df.columns:
            df['RD Feedback'] = ''
        
        # Get existing feedbacks (if any)
        current_feedback = df.at[row_idx, 'RD Feedback']
        feedback_list = []
        
        if pd.notna(current_feedback) and str(current_feedback).strip():
            try:
                feedback_str = str(current_feedback).strip()
                if feedback_str.startswith('['):
                    # It's already a list
                    feedback_list = json.loads(feedback_str)
                elif feedback_str.startswith('{'):
                    # It's a single object, convert to list
                    feedback_list = [json.loads(feedback_str)]
            except (json.JSONDecodeError, ValueError):
                # If parsing fails, start with empty list
                feedback_list = []
        
        # Append new feedback to the list
        feedback_list.append(new_feedback)
        
        # Save back as JSON array
        df.at[row_idx, 'RD Feedback'] = json.dumps(feedback_list, ensure_ascii=False)
        df.to_csv(csv_path, index=False, encoding='utf-8')
        
        return True, "Feedback added successfully"
        
    except Exception as e:
        return False, f"Error: {str(e)}"


@app.route('/api/add-review', methods=['POST'])
def add_review():
    """API endpoint to add feedback."""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        foodlog_id = data.get('foodlog_id', '').strip()
        rd_name = data.get('rd_name', '').strip()
        rd_feedback = data.get('rd_feedback', '').strip()
        
        if not foodlog_id:
            return jsonify({'success': False, 'error': 'FoodLogId is required'}), 400
        
        if not rd_name:
            return jsonify({'success': False, 'error': 'RD name is required'}), 400
        
        if not rd_feedback:
            return jsonify({'success': False, 'error': 'RD feedback is required'}), 400
        
        success, message = add_review_to_csv(foodlog_id, rd_name, rd_feedback)
        
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 400
            
    except Exception as e:
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'}), 500


@app.route('/gallery')
@app.route('/')
def index():
    """Serve dynamically generated HTML gallery (real-time from CSV)."""
    html_content = generate_gallery_html()
    return Response(html_content, mimetype='text/html')


@app.route('/<path:filename>')
def serve_static(filename):
    """Serve static HTML files from the HTML directory (backward compatibility)."""
    global html_dir
    
    # Don't serve gallery routes as static files
    if filename in ['gallery', '']:
        return index()
    
    # Try to serve from html_dir if it exists
    if html_dir and (html_dir / filename).exists():
        return send_from_directory(html_dir, filename)
    
    return "File not found", 404


def main():
    """Main function to start the Flask server."""
    global csv_path, html_dir, images_dir
    
    parser = argparse.ArgumentParser(
        description="Start Flask server for RD feedback submission with dynamic HTML generation"
    )
    parser.add_argument(
        '--csv', 
        default='./foodlog_ai_analysis_v3.csv',
        help='CSV file path'
    )
    parser.add_argument(
        '--images',
        default='./images',
        help='Images directory (default: ./images)'
    )
    parser.add_argument(
        '--html-dir',
        default='.',
        help='Directory containing static HTML files (for backward compatibility)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5000,
        help='Port number (default: 5000)'
    )
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='Host address (default: 127.0.0.1)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode'
    )
    
    args = parser.parse_args()
    
    csv_path = Path(args.csv)
    images_dir = Path(args.images)
    html_dir = Path(args.html_dir)
    
    if not csv_path.exists():
        print(f"[ERROR] CSV file does not exist: {csv_path}", file=sys.stderr)
        sys.exit(1)
    
    if not images_dir.exists():
        print(f"[WARN] Images directory does not exist: {images_dir}", file=sys.stderr)
    
    print(f"[INFO] CSV file: {csv_path.resolve()}")
    print(f"[INFO] Images directory: {images_dir.resolve()}")
    
    # Generate and save gallery.html file
    # 生成并保存 gallery.html 文件
    try:
        html_content = generate_gallery_html()
        gallery_html_path = Path("gallery.html")
        gallery_html_path.write_text(html_content, encoding="utf-8")
        print(f"[INFO] Saved gallery.html: {gallery_html_path.resolve()}")
    except Exception as e:
        print(f"[WARN] Failed to save gallery.html: {e}", file=sys.stderr)
    
    print(f"[INFO] Starting server on http://{args.host}:{args.port}")
    print(f"[INFO] Open http://{args.host}:{args.port}/gallery in your browser")
    print(f"[INFO] Gallery is dynamically generated from CSV (real-time updates)")
    
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
