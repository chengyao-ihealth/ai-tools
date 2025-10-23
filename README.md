# AI Tools - Food Log Gallery Generator

Tools for downloading food log images and generating HTML galleries from CSV data.

## Features

- **Image Download**: Download images from production database
- **Gallery Generation**: Create responsive HTML galleries with formatted data
- **Dynamic Columns**: Auto-detect and display all CSV columns
- **Multi-format Support**: Compatible with different CSV structures
- **Self-contained**: Images embedded as data URIs

## Tools

### 1. Download Images (`download_images.py`)
```bash
# Setup: Replace token and csv_path in the script
python3 download_images.py
```

### 2. Gallery Generator (`show_foodlog_gallery.py`)
```bash
# Direct file input (recommended)
python3 show_foodlog_gallery.py your_data.csv

# With options
python3 show_foodlog_gallery.py your_data.csv --out gallery.html --title "My Gallery" --open
```

**Required CSV Column:**
- `ImgName`: Image filenames (multiple separated by semicolon)

**Supported Formats:**
- Any CSV with `ImgName` column
- Auto-detects and displays all other columns
- Excludes system columns (MemberId, FoodLogId)

## Quick Start

```bash
# 1. Download images
python3 download_images.py

# 2. Generate gallery
python3 show_foodlog_gallery.py your_data.csv --open
```

## Requirements

- Python 3.6+
- pandas
- requests (for image download)
