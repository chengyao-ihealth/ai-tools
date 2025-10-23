# AI Tools - Food Log Gallery Generator

This repository contains tools for downloading food log images and generating beautiful HTML galleries from CSV data.

这个仓库包含用于下载食物记录图片并从CSV数据生成美观HTML画廊的工具。

## Features / 功能特性

- **Image Download**: Download food log images from production database / 从生产数据库下载食物记录图片
- **Gallery Generation**: Create responsive HTML galleries with formatted data / 创建带格式化数据的响应式HTML画廊
- **Self-contained HTML**: Images embedded as data URIs (no external dependencies) / 图片嵌入为data URI（无外部依赖）
- **Bilingual Support**: English and Chinese documentation / 英文和中文文档
- **Modern UI**: Clean, responsive design with CSS Grid / 使用CSS Grid的简洁响应式设计

## Tools / 工具

### 1. Download Images (`download_images.py`)

Downloads food log images from the production database based on CSV data.

根据CSV数据从生产数据库下载食物记录图片。

**Setup / 设置:**
1. Replace the variable 'token' with yours (Can get it from https://portal.ihealthunifiedcare.com/care-portal/home -> inspect -> application -> session storage -> https://portal.ihealth-eng.com -> token)
2. Put your .csv file (with column 'FoodLogId') in the folder and change the variable 'csv_path' to your .csv file name (or rename your .csv as foodlog_ai_analysis_img_name.csv)
3. Run "python3 download_images.py"

### 2. Gallery Generator (`show_foodlog_gallery.py`)

Generates beautiful HTML galleries from CSV food log data with images and formatted JSON fields.

从CSV食物记录数据生成美观的HTML画廊，包含图片和格式化的JSON字段。

**Setup / 设置:**
1. Put your .csv file (with all the required columns) in the folder and change the variable 'csv_path' to your .csv file name (or rename your .csv as foodlog_ai_analysis_img_name.csv)
2. Run "python3 show_foodlog_gallery.py"

**Usage / 使用方法:**
```bash
# Basic usage / 基本用法
python show_foodlog_gallery.py

# With custom parameters / 使用自定义参数
python show_foodlog_gallery.py --csv your_data.csv --images ./images --out gallery.html --title "My Food Gallery" --open
```

**Required CSV Columns / 必需的CSV列:**
- `ImgName`: Image filenames (can be multiple, separated by semicolon) / 图片文件名（可多个，用分号分隔）
- `MealTitle`: Title of the meal / 餐食标题
- `Description`: Meal description / 餐食描述
- `RD Comments`: Nutritionist comments (JSON format) / 营养师评论（JSON格式）
- `Insight`: AI insights / AI洞察
- `Ingredients`: Ingredient details (JSON format) / 食材详情（JSON格式）
- `FoodLogId`: Unique identifier / 唯一标识符

## Gallery Features / 画廊功能

- **Responsive Grid Layout**: Automatically adjusts to screen size / 自动适应屏幕尺寸
- **Image Support**: Multiple images per food log entry / 每个食物记录支持多张图片
- **Formatted JSON**: RD Comments and Ingredients displayed in readable format / RD评论和食材以可读格式显示
- **Self-contained**: All images embedded as data URIs / 所有图片嵌入为data URI
- **Modern Styling**: Clean, professional appearance / 简洁专业的外观
- **Search Friendly**: Use browser search (⌘/Ctrl+F) to find specific entries / 使用浏览器搜索（⌘/Ctrl+F）查找特定条目

## File Structure / 文件结构

```
ai-tools/
├── download_images.py          # Image download script / 图片下载脚本
├── show_foodlog_gallery.py     # Gallery generator / 画廊生成器
├── show_foodlog_gallery_v0.py  # Original version / 原始版本
├── images/                     # Downloaded images directory / 下载的图片目录
├── *.csv                       # CSV data files / CSV数据文件
└── *.html                      # Generated gallery files / 生成的画廊文件
```

## Requirements / 要求

- Python 3.6+
- pandas
- requests (for image download)

## Author / 作者

Created by Chengyao / 由Chengyao创建
