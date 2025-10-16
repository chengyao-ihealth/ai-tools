# show_foodlog_gallery.py
"""
FoodLog Gallery Generator
Generate an HTML gallery from CSV food log data with images and formatted JSON fields.
从 CSV 食物记录数据生成 HTML 画廊，包含图片和格式化的 JSON 字段。
"""
import argparse
import base64
import json
import os
import sys
import html
import webbrowser
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


def looks_like_json(s: str) -> bool:
    """
    Check if a string looks like JSON (starts with { or [ and ends with } or ]).
    检查字符串是否看起来像 JSON（以 { 或 [ 开头，以 } 或 ] 结尾）。
    """
    s = s.strip()
    return (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))


def humanize_json(obj: Any, *, indent_level: int = 0) -> str:
    """
    Convert dict/list/primitive types to natural language string.
    将字典/列表/原子类型转为自然语言字符串。
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


def format_rd_comments(s: Any) -> str:
    """
    Format RD Comments to show only 'text' and 'commentedAt' fields.
    格式化 RD Comments，只显示 'text' 和 'commentedAt' 字段。
    Format: Comment: {text}\nTime: {commentedAt}
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
    Format:
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


def json_to_text(s: Any) -> str:
    """
    Convert input (JSON string, parsed object, or plain string) to natural language text.
    输入可能是 JSON 字符串、已解析对象或普通字符串。
    Returns natural language text (no HTML tags).
    返回自然语言文本（不含 HTML 标签）。
    """
    if s is None:
        return ""
    if isinstance(s, (dict, list)):
        return humanize_json(s)
    if isinstance(s, str):
        s_strip = s.strip()
        if not s_strip:
            return ""
        if looks_like_json(s_strip):
            try:
                obj = json.loads(s_strip)
                return humanize_json(obj)
            except Exception:
                # Not valid JSON, return as-is / 不是合法 JSON，就原样返回
                return s_strip
        return s_strip
    return str(s)


def read_image_as_data_uri(img_path: Path) -> str:
    """
    Convert local image to data URI (self-contained HTML, no file path dependencies).
    将本地图片转为 data URI（这样生成的 HTML 是自包含的，不依赖文件路径）。
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


def build_card_html(row, images_dir: Path) -> str:
    """
    Build HTML card for a single food log entry.
    构建单个食物记录的 HTML 卡片。
    """
    meal_title = str(row.get("MealTitle", "") or "").strip()
    description = str(row.get("Description", "") or "").strip()
    insight = str(row.get("Insight", "") or "").strip()
    # Use specialized formatters for RD Comments and Ingredients
    # 对 RD Comments 和 Ingredients 使用专用格式化器
    rd_comments = format_rd_comments(row.get("RD Comments", ""))
    ingredients = format_ingredients(row.get("Ingredients", ""))
    foodlog_id = str(row.get("FoodLogId", "") or "").strip()

    # Handle multiple images: ImgName can be "fid.jpg;fid_1.png"
    # 处理多图：ImgName 可能是 "fid.jpg;fid_1.png"
    raw_imgnames = str(row.get("ImgName", "") or "").strip()
    img_names: Iterable[str] = [x.strip() for x in raw_imgnames.split(";") if x.strip()] if raw_imgnames else []

    img_tags = []
    if img_names:
        for name in img_names:
            img_path = images_dir / name
            # Use relative path instead of data URI / 使用相对路径而不是 data URI
            if img_path.exists():
                # Create relative path from images directory / 创建从 images 目录的相对路径
                relative_path = f"{images_dir.name}/{name}"
                img_tags.append(f'<img src="{html.escape(relative_path)}" alt="{html.escape(name)}" />')
            else:
                img_tags.append(f'<div class="img-missing">缺失：{html.escape(name)}</div>')
    else:
        img_tags.append('<div class="img-missing">未提供图片文件名</div>')

    def para(label: str, text: str, escape_html: bool = True) -> str:
        """Helper to create a field div with label and value."""
        if not text:
            return ""
        if escape_html:
            safe = html.escape(text).replace("\n", "<br/>")
        else:
            # Don't escape HTML, but still convert newlines to <br/>
            safe = text.replace("\n", "<br/>")
        return f'<div class="field"><div class="label">{html.escape(label)}</div><div class="value">{safe}</div></div>'

    return f"""
    <div class="card">
        <div class="images">
            {''.join(img_tags)}
        </div>
        <div class="meta">
            {para("FoodLogId", foodlog_id)}
            {para("MealTitle", meal_title)}
            {para("Description", description)}
            {para("Insight", insight)}
            {para("RD Comments", rd_comments)}
            {para("Ingredients", ingredients, escape_html=False)}
        </div>
    </div>
    """


def build_html(doc_cards: str, title: str = "FoodLog Gallery") -> str:
    """
    Build complete HTML document with card grid layout.
    构建完整的 HTML 文档，使用卡片网格布局。
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
</style>
</head>
<body>
  <div class="header">
    <h1>{html.escape(title)}</h1>
    <div class="hint">by Chengyao</div>
  </div>
  <div class="grid">
  {doc_cards}
  </div>
  <div class="footer">Tip：若图片过多，可在浏览器中使用搜索（⌘/Ctrl+F）按 MealTitle 或 FoodLogId 快速定位。</div>
</body>
</html>
"""


def main():
    """
    Main function to generate HTML gallery from CSV food log data.
    主函数，从 CSV 食物记录数据生成 HTML 画廊。
    """
    parser = argparse.ArgumentParser(
        description="Load images from specified directory based on CSV ImgName, display them, and convert JSON columns to natural language. / 根据 CSV 的 ImgName 在指定 images 目录加载图片，展示并把 JSON 列转为自然语言。"
    )
    parser.add_argument("--csv", default="./foodlog_ai_analysis_img_name_10.csv", help="CSV file path (must contain columns: ImgName, MealTitle, Description, RD Comments, Insight, Ingredients) / CSV 文件路径（需包含列：ImgName, MealTitle, Description, RD Comments, Insight, Ingredients）")
    parser.add_argument("--images", default="./images", help="Images directory (default: ./images) / 图片目录（默认 ./images）")
    parser.add_argument("--out", default="gallery.html", help="Output HTML filename (default: gallery.html) / 输出 HTML 文件名（默认 gallery.html）")
    parser.add_argument("--title", default="FoodLog Gallery", help="HTML page title / HTML 页面标题")
    parser.add_argument("--open", action="store_true", help="Automatically open in default browser after generation / 生成后自动在默认浏览器打开")
    args = parser.parse_args()

    csv_path = Path(args.csv)
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
    needed_cols = ["ImgName", "MealTitle", "Description", "RD Comments", "Insight", "Ingredients"]
    missing = [c for c in needed_cols if c not in df.columns]
    if missing:
        print(f"[ERROR] Missing columns / 缺少列：{missing}", file=sys.stderr)
        sys.exit(1)

    # Generate all cards / 生成所有卡片
    cards_html = []
    total = len(df)
    for _, row in df.iterrows():
        try:
            cards_html.append(build_card_html(row, images_dir))
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
