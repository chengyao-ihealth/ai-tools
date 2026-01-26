# show_foodlog_gallery.py
"""
Flexible FoodLog Gallery Generator
Generate an HTML gallery from CSV food log data with dynamic column support.
灵活的CSV食物记录数据生成HTML画廊，支持动态列显示。

This script automatically detects CSV columns and displays all available fields
except for system columns (MemberId, FoodLogId). Only ImgName is required for images.

这个脚本自动检测CSV列并显示所有可用字段（除了系统列MemberId, FoodLogId）。
只有ImgName是必需的，用于显示图片。

Features / 功能特性:
- Dynamic column detection and display / 动态列检测和显示
- Automatic JSON formatting for complex fields / 复杂字段的自动JSON格式化
- Backward compatibility with existing CSV formats / 与现有CSV格式的向后兼容
- Flexible image handling / 灵活的图片处理
- Self-contained HTML output / 自包含HTML输出

Usage / 使用方法:
    python show_foodlog_gallery.py --csv your_data.csv --images ./images --out gallery.html --open

Required CSV columns / 必需的CSV列:
- ImgName: Image filenames (can be multiple, separated by semicolon) / 图片文件名（可多个，用分号分隔）

Optional columns / 可选列:
- Any other columns will be automatically displayed / 任何其他列都会自动显示
- System columns (MemberId, FoodLogId) are excluded / 系统列（MemberId, FoodLogId）被排除
"""
import argparse
import base64
import json
import os
import re
import sys
import html
import webbrowser
from pathlib import Path
from typing import Any, Iterable, Dict, List

import pandas as pd


def looks_like_json(s: str) -> bool:
    """
    Check if a string looks like JSON (starts with { or [ and ends with } or ]).
    检查字符串是否看起来像 JSON（以 { 或 [ 开头，以 } 或 ] 结尾）。
    
    This is a simple heuristic to determine if a string might be JSON data
    before attempting to parse it with json.loads().
    
    这是一个简单的启发式方法，用于在尝试用json.loads()解析之前
    判断字符串是否可能是JSON数据。
    
    Args:
        s (str): The string to check / 要检查的字符串
        
    Returns:
        bool: True if string looks like JSON / 如果字符串看起来像JSON则返回True
    """
    s = s.strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))


def humanize_json(obj: Any, *, indent_level: int = 0) -> str:
    """
    Convert dict/list/primitive types to natural language string.
    将字典/列表/原子类型转为自然语言字符串。
    
    This function recursively converts JSON objects into human-readable text format.
    It handles nested structures and provides proper indentation for readability.
    
    这个函数递归地将JSON对象转换为人类可读的文本格式。
    它处理嵌套结构并提供适当的缩进以提高可读性。
    
    Args:
        obj (Any): The object to convert (dict, list, or primitive) / 要转换的对象（字典、列表或原始类型）
        indent_level (int): Current indentation level for nested structures / 嵌套结构的当前缩进级别
        
    Returns:
        str: Human-readable text representation / 人类可读的文本表示
        
    Examples / 示例:
        - dict: "key: value" for each item / 逐项 "键：值"
        - list: comma separated; if contains dict, show as multi-line items / 逗号分隔；若包含 dict，则转为分行的项目列表
        - other: convert to str / 其他: 直接转 str
    """
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
        # If list contains dicts, show as multi-line items
        # 如果列表里是字典，分行展示每一项
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
        # Otherwise join with commas / 否则用顿号/逗号连接
        return "，".join(str(x) for x in obj)
    # Primitive types / 原子类型
    return str(obj)


def format_field_value(value: Any, field_name: str) -> str:
    """
    Format a field value based on its content and field name.
    根据字段内容和字段名称格式化字段值。
    
    This function provides intelligent formatting for different types of data:
    - JSON strings are parsed and humanized
    - Special field names get custom formatting
    - Regular text is returned as-is
    
    这个函数为不同类型的数据提供智能格式化：
    - JSON字符串被解析并人性化
    - 特殊字段名称获得自定义格式化
    - 常规文本原样返回
    
    Args:
        value (Any): The field value to format / 要格式化的字段值
        field_name (str): The name of the field / 字段名称
        
    Returns:
        str: Formatted field value / 格式化的字段值
    """
    if value is None:
        return ""
    
    # Convert to string if not already
    if not isinstance(value, str):
        value = str(value)
    
    value = value.strip()
    if not value:
        return ""
    
    # Special handling for known field types
    # 对已知字段类型的特殊处理
    if field_name.lower() in ['rd comments', 'rd_comments']:
        return format_rd_comments(value)
    elif field_name.lower() in ['ingredients']:
        return format_ingredients(value)
    elif field_name.lower() in ['aiinsight', 'insight']:
        return value  # Keep as-is for insights
    elif field_name.lower() in ['aititle', 'mealtitle']:
        return value  # Keep as-is for titles
    elif field_name.lower() in ['description']:
        return value  # Keep as-is for descriptions
    # Special handling for MicroAction - expand abbreviations to full names
    # 特殊处理 MicroAction - 将缩写扩展为全名
    elif field_name.lower() == 'microaction':
        # Map abbreviations to full names
        # 将缩写映射为全名
        microaction_map = {
            'EO': 'EATING_ORDER',
            'CA': 'COMPOSITION_ADDITION',
            'SUB': 'SUBSTITUTION',
            'PAIR': 'PAIRING',
            'MOVE': 'GENTLE_MOVEMENT'
        }
        # Replace abbreviations (case-insensitive)
        # 替换缩写（不区分大小写）
        result = value
        for abbrev, full_name in microaction_map.items():
            # Replace whole word matches (case-insensitive)
            # 替换整个单词匹配（不区分大小写）
            pattern = r'\b' + re.escape(abbrev) + r'\b'
            result = re.sub(pattern, full_name, result, flags=re.IGNORECASE)
        return result
    
    # Check if it looks like JSON and try to parse
    # 检查是否看起来像JSON并尝试解析
    if looks_like_json(value):
        try:
            obj = json.loads(value)
            return humanize_json(obj)
        except Exception:
            # If JSON parsing fails, return as-is
            # 如果JSON解析失败，原样返回
            return value
    
    return value


def format_rd_comments(s: Any) -> str:
    """
    Format RD Comments to show only 'text' and 'commentedAt' fields.
    格式化 RD Comments，只显示 'text' 和 'commentedAt' 字段。
    
    This function specifically handles the RD Comments field which contains nutritionist
    feedback in JSON format. It extracts only the essential information (comment text
    and timestamp) and formats it in a readable way.
    
    这个函数专门处理RD Comments字段，该字段包含营养师反馈的JSON格式数据。
    它只提取关键信息（评论文本和时间戳）并以可读的方式格式化。
    
    Args:
        s (Any): RD Comments data (can be JSON string, dict, list, or plain text) / RD评论数据（可以是JSON字符串、字典、列表或纯文本）
        
    Returns:
        str: Formatted comment text with timestamps / 格式化的评论文本和时间戳
        
    Format / 格式:
        Comment: {text}
        Time: {commentedAt}
    """
    if s is None:
        return ""
    
    # Parse JSON if it's a string
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
    
    # Handle list of comments
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
    
    # Handle single comment dict
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
    """
    Format Ingredients with structured display format.
    格式化 Ingredients 为结构化显示格式。
    
    This function handles the Ingredients field which contains detailed information
    about food ingredients including names, portions, nutrition facts, and calories.
    It creates a structured, readable format with proper HTML formatting.
    
    这个函数处理Ingredients字段，该字段包含食材的详细信息，
    包括名称、分量、营养信息和卡路里。它创建结构化的、可读的格式，并包含适当的HTML格式。
    
    Args:
        s (Any): Ingredients data (can be JSON string, dict, list, or plain text) / 食材数据（可以是JSON字符串、字典、列表或纯文本）
        
    Returns:
        str: Formatted ingredients with HTML tags for styling / 格式化的食材信息，包含HTML标签用于样式
        
    Format / 格式:
        **1. {name}** (bold / 加粗)
        Estimated Portion: {estimatedPortion}
        Nutrition:
        - {NUTRITION}: {gram}g
        kcalPer100g: {kcalPer100g}
    """
    if s is None:
        return ""
    
    # Parse JSON if it's a string
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
    
    # Handle list of ingredients
    if isinstance(obj, list):
        if not obj:
            return ""
        result_lines = []
        for idx, ingredient in enumerate(obj, 1):
            if isinstance(ingredient, dict):
                ingredient_lines = []
                
                # Ingredient number and name (in bold using <strong> tag)
                name = ingredient.get("name", "")
                if name:
                    ingredient_lines.append(f"<strong>{idx}. {name}</strong>")
                else:
                    ingredient_lines.append(f"<strong>{idx}. Unknown ingredient</strong>")
                
                # Estimated Portion
                portion = ingredient.get("estimatedPortion", ingredient.get("Portion", ""))
                if portion:
                    ingredient_lines.append(f"Estimated Portion: {portion}")
                
                # Nutrition details
                nutrition_data = ingredient.get("nutrition", [])
                if nutrition_data:
                    ingredient_lines.append("Nutrition:")
                    # If nutrition is a list of dicts
                    if isinstance(nutrition_data, list):
                        for nutr_item in nutrition_data:
                            if isinstance(nutr_item, dict):
                                nutr_name = nutr_item.get("nutrition", "")
                                gram = nutr_item.get("gram", "")
                                if nutr_name:
                                    ingredient_lines.append(f"  - {nutr_name}: {gram}g")
                
                # Kcal per 100g
                kcal = ingredient.get("kcalPer100g", "")
                if kcal:
                    ingredient_lines.append(f"kcalPer100g: {kcal}")
                
                result_lines.append("\n".join(ingredient_lines))
        
        return "\n\n".join(result_lines)
    
    # Handle single ingredient dict
    if isinstance(obj, dict):
        lines = []
        
        # Ingredient name (in bold)
        name = obj.get("name", "")
        if name:
            lines.append(f"<strong>1. {name}</strong>")
        
        # Estimated Portion
        portion = obj.get("estimatedPortion", obj.get("Portion", ""))
        if portion:
            lines.append(f"Estimated Portion: {portion}")
        
        # Nutrition details
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
        
        # Kcal per 100g
        kcal = obj.get("kcalPer100g", "")
        if kcal:
            lines.append(f"kcalPer100g: {kcal}")
        
        return "\n".join(lines)
    
    return str(obj)


def read_image_as_data_uri(img_path: Path) -> str:
    """
    Convert local image to data URI (self-contained HTML, no file path dependencies).
    将本地图片转为 data URI（这样生成的 HTML 是自包含的，不依赖文件路径）。
    
    This function reads an image file from the local filesystem and converts it to a
    base64-encoded data URI. This allows the generated HTML to be completely self-contained
    without requiring external image files.
    
    这个函数从本地文件系统读取图片文件并将其转换为base64编码的data URI。
    这使得生成的HTML完全自包含，不需要外部图片文件。
    
    Args:
        img_path (Path): Path to the image file / 图片文件路径
        
    Returns:
        str: Data URI string (e.g., "data:image/jpeg;base64,...") or empty string if failed / 
             Data URI字符串（例如："data:image/jpeg;base64,..."）或失败时返回空字符串
        
    Supported formats / 支持的格式:
        - .jpg, .jpeg: image/jpeg
        - .png: image/png  
        - .gif: image/gif
        - .webp: image/webp
    """
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
        # If cannot read, return empty string, will show "image missing" / 读不到就返回空串，后续会显示"图片缺失"
        return ""


def get_display_columns(df: pd.DataFrame) -> List[str]:
    """
    Get the list of columns to display, excluding system columns.
    获取要显示的列列表，排除系统列。
    
    Args:
        df (pd.DataFrame): The DataFrame to analyze / 要分析的DataFrame
        
    Returns:
        List[str]: List of column names to display / 要显示的列名列表
    """
    # System columns to exclude from display
    # 从显示中排除的系统列
    system_columns = {'MemberId', 'FoodLogId'}
    
    # Get all columns except system ones
    # 获取除系统列之外的所有列
    display_columns = [col for col in df.columns if col not in system_columns]
    
    return display_columns


def build_card_html(row, images_dir: Path, display_columns: List[str], row_idx: Any = None) -> str:
    """
    Build HTML card for a single food log entry with dynamic columns.
    为单个食物记录构建HTML卡片，支持动态列。
    
    This function takes a single row from the CSV data and creates a complete HTML card
    that displays all the food log information including images and all available fields.
    
    这个函数获取CSV数据中的单行记录，创建一个完整的HTML卡片，
    显示所有食物记录信息，包括图片和所有可用字段。
    
    Args:
        row: Pandas DataFrame row containing food log data / 包含食物记录数据的Pandas DataFrame行
        images_dir (Path): Directory containing the image files / 包含图片文件的目录
        display_columns (List[str]): List of columns to display / 要显示的列列表
        
    Returns:
        str: Complete HTML card markup / 完整的HTML卡片标记
    """
    # Handle images first (ImgName is required)
    # 首先处理图片（ImgName是必需的）
    raw_imgnames = str(row.get("ImgName", "") or "").strip()
    img_names: Iterable[str] = [x.strip() for x in raw_imgnames.split(";") if x.strip()] if raw_imgnames else []

    # Process each image: convert to data URI or show missing placeholder
    # 处理每张图片：转换为data URI或显示缺失占位符
    img_tags = []
    if img_names:
        for name in img_names:
            img_path = images_dir / name
            data_uri = read_image_as_data_uri(img_path)
            if data_uri:
                # Successfully loaded image, create img tag with data URI
                # 成功加载图片，创建带data URI的img标签
                img_tags.append(f'<img src="{data_uri}" alt="{html.escape(name)}" />')
            else:
                # Image file not found or failed to load, show missing placeholder
                # 图片文件未找到或加载失败，显示缺失占位符
                img_tags.append(f'<div class="img-missing">缺失：{html.escape(name)}</div>')
    else:
        # No image names provided in CSV
        # CSV中未提供图片名称
        img_tags.append('<div class="img-missing">未提供图片文件名</div>')

    def para(label: str, text: str, escape_html: bool = True) -> str:
        """
        Helper to create a field div with label and value.
        创建带标签和值的字段div的辅助函数。
        
        Args:
            label (str): Field label / 字段标签
            text (str): Field value / 字段值
            escape_html (bool): Whether to escape HTML in the value / 是否转义值中的HTML
            
        Returns:
            str: HTML div element / HTML div元素
        """
        if not text:
            return ""
        if escape_html:
            # Escape HTML characters and convert newlines to <br/> tags
            # 转义HTML字符并将换行符转换为<br/>标签
            safe = html.escape(text).replace("\n", "<br/>")
        else:
            # Don't escape HTML, but still convert newlines to <br/>
            # 不转义HTML，但仍将换行符转换为<br/>
            safe = text.replace("\n", "<br/>")
        return f'<div class="field"><div class="label">{html.escape(label)}</div><div class="value">{safe}</div></div>'

    # Generate field HTML for all display columns
    # 为所有显示列生成字段HTML
    field_html = []
    for col in display_columns:
        if col == "ImgName":
            continue  # Skip ImgName as it's handled separately
        
        value = row[col] if col in row.index else ""
        formatted_value = format_field_value(value, col)
        
        # Determine if this field should allow HTML (like Ingredients)
        # 确定此字段是否应允许HTML（如Ingredients）
        allow_html = col.lower() in ['ingredients']
        
        if formatted_value:
            field_html.append(para(col, formatted_value, escape_html=not allow_html))

    # Get FoodLogId for form identification (use index if FoodLogId not available)
    # 获取FoodLogId用于表单标识（如果FoodLogId不可用，使用索引）
    foodlog_id = ""
    if "FoodLogId" in row.index:
        foodlog_id_val = row["FoodLogId"]
        foodlog_id = str(foodlog_id_val) if pd.notna(foodlog_id_val) else ""
    if not foodlog_id and row_idx is not None:
        # Fallback: use row index as identifier
        # 备用方案：使用行索引作为标识符
        foodlog_id = str(row_idx)

    # Add review form
    # 添加review表单（用于标注AI生成内容的质量）
    review_form = f"""
        <div class="review-form">
            <div class="review-form-title">Add RD Review</div>
            <div class="review-form-hint">For labeling the quality of AI-generated content</div>
            <form class="rd-review-form" data-foodlog-id="{html.escape(foodlog_id)}">
                <div class="form-group">
                    <label for="rd-name-{html.escape(foodlog_id)}">RD Name:</label>
                    <input type="text" id="rd-name-{html.escape(foodlog_id)}" name="rd_name" class="form-input" required />
                </div>
                <div class="form-group">
                    <label for="rd-review-{html.escape(foodlog_id)}">Review (AI Content Quality Assessment):</label>
                    <textarea id="rd-review-{html.escape(foodlog_id)}" name="rd_review" class="form-textarea" rows="3" placeholder="Please assess the quality of AI-generated content..." required></textarea>
                </div>
                <button type="submit" class="submit-btn">Submit</button>
                <div class="form-status"></div>
            </form>
            <div class="review-display" id="review-display-{html.escape(foodlog_id)}"></div>
        </div>
    """

    return f"""
    <div class="card" data-foodlog-id="{html.escape(foodlog_id)}">
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
    """
    Build complete HTML document with card grid layout.
    构建完整的 HTML 文档，使用卡片网格布局。
    
    This function creates a complete HTML document with modern CSS styling,
    responsive grid layout, and all the food log cards embedded within it.
    
    这个函数创建一个完整的HTML文档，包含现代化CSS样式、
    响应式网格布局，以及嵌入其中的所有食物记录卡片。
    
    Args:
        doc_cards (str): HTML content for all food log cards / 所有食物记录卡片的HTML内容
        title (str): Page title / 页面标题
        
    Returns:
        str: Complete HTML document / 完整的HTML文档
        
    Features / 功能特性:
        - Responsive grid layout / 响应式网格布局
        - Modern CSS with CSS variables / 使用CSS变量的现代化CSS
        - Light theme with clean design / 简洁设计的浅色主题
        - Mobile-friendly / 移动端友好
        - Self-contained (no external dependencies) / 自包含（无外部依赖）
    """
    # Simple card grid style / 简单的卡片网格样式
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
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
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
.field .label {{
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 2px;
}}
.field .value {{
  font-size: 14px; line-height: 1.5;
  white-space: normal;
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
  border-radius: 6px; display: none;
}}
.review-display.show {{
  display: block;
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
    <h1>{html.escape(title)}</h1>
    <div class="hint">by Chengyao </div>
  </div>
  <div class="grid">
  {doc_cards}
  </div>
  <div class="footer">Tip：若图片过多，可在浏览器中使用搜索（⌘/Ctrl+F）按字段内容快速定位。</div>
<script>
// Helper function to escape HTML
// 转义HTML的辅助函数
function escapeHtml(text) {{
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}}

// Handle form submissions
document.addEventListener('DOMContentLoaded', function() {{
    const forms = document.querySelectorAll('.rd-review-form');
    
    forms.forEach(function(form) {{
        form.addEventListener('submit', async function(e) {{
            e.preventDefault();
            
            const foodlogId = form.getAttribute('data-foodlog-id');
            const rdName = form.querySelector('input[name="rd_name"]').value.trim();
            const rdReview = form.querySelector('textarea[name="rd_review"]').value.trim();
            const submitBtn = form.querySelector('.submit-btn');
            const statusDiv = form.querySelector('.form-status');
            
            if (!rdName || !rdReview) {{
                statusDiv.textContent = 'Please fill in RD name and review';
                statusDiv.className = 'form-status error';
                return;
            }}
            
            // Disable submit button
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
                        rd_review: rdReview
                    }})
                }});
                
                const result = await response.json();
                
                if (response.ok && result.success) {{
                    statusDiv.textContent = 'Success! Review saved.';
                    statusDiv.className = 'form-status success';
                    
                    // Display the review on the page
                    // 在页面上显示review
                    const reviewDisplay = document.getElementById('review-display-' + foodlogId);
                    if (reviewDisplay) {{
                        const escapedName = escapeHtml(rdName);
                        const escapedReview = escapeHtml(rdReview);
                        const timestamp = new Date().toLocaleString();
                        reviewDisplay.innerHTML = '<div class="review-display-header">RD Review:</div><div class="review-display-content">' + escapedReview + '</div><div class="review-display-meta">By: ' + escapedName + ' | ' + timestamp + '</div>';
                        reviewDisplay.classList.add('show');
                    }}
                    
                    // Clear form
                    form.querySelector('input[name="rd_name"]').value = '';
                    form.querySelector('textarea[name="rd_review"]').value = '';
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


def main():
    """
    Main function to generate HTML gallery from CSV food log data.
    主函数，从 CSV 食物记录数据生成 HTML 画廊。
    """
    parser = argparse.ArgumentParser(
        description="Load images from specified directory based on CSV ImgName, display them, and convert JSON columns to natural language. Supports dynamic column detection. / 根据 CSV 的 ImgName 在指定 images 目录加载图片，展示并把 JSON 列转为自然语言。支持动态列检测。"
    )
    parser.add_argument("csv_file", nargs='?', default="./foodlog_ai_analysis_v3.csv", help="CSV file path (must contain ImgName column) / CSV 文件路径（必须包含ImgName列）")
    parser.add_argument("--images", default="./images", help="Images directory (default: ./images) / 图片目录（默认 ./images）")
    parser.add_argument("--out", default="gallery_flexible.html", help="Output HTML filename (default: gallery_flexible.html) / 输出 HTML 文件名（默认 gallery_flexible.html）")
    parser.add_argument("--title", default="FoodLog Gallery - Flexible", help="HTML page title / HTML 页面标题")
    parser.add_argument("--open", action="store_true", help="Automatically open in default browser after generation / 生成后自动在默认浏览器打开")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    images_dir = Path(args.images)
    out_html = Path(args.out)

    # Check if CSV file exists / 检查 CSV 文件是否存在
    if not csv_path.exists():
        print(f"[ERROR] CSV does not exist / CSV 不存在：{csv_path}", file=sys.stderr)
        sys.exit(1)

    # Read CSV file / 读取 CSV 文件
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[ERROR] Failed to read CSV / 读取 CSV 失败：{e}", file=sys.stderr)
        sys.exit(1)

    # Validate required columns / 校验必要列
    if "ImgName" not in df.columns:
        print(f"[ERROR] Missing required column: ImgName / 缺少必需列：ImgName", file=sys.stderr)
        sys.exit(1)

    # Get display columns (exclude system columns)
    # 获取显示列（排除系统列）
    display_columns = get_display_columns(df)
    print(f"[INFO] Display columns / 显示列：{display_columns}")

    # Generate all cards / 生成所有卡片
    cards_html = []
    total = len(df)
    for idx, row in df.iterrows():
        try:
            cards_html.append(build_card_html(row, images_dir, display_columns, row_idx=idx))
        except Exception as e:
            # Continue even if single record fails / 即使单条失败也不中断
            print(f"[WARN] Failed to render a record / 渲染某条记录失败：{e}", file=sys.stderr)

    # Build and write HTML document / 构建并写入 HTML 文档
    doc = build_html("".join(cards_html), title=args.title)
    out_html.write_text(doc, encoding="utf-8")
    print(f"[OK] Generated / 已生成：{out_html.resolve()} (Total / 共 {total} 条)")

    # Open in browser if requested / 如果请求则在浏览器中打开
    if args.open:
        webbrowser.open(out_html.resolve().as_uri())


if __name__ == "__main__":
    main()
