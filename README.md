## Installation

```bash
# Install all dependencies
pip install -r requirements.txt

# Or install individually
pip install pandas httpx requests pymongo python-dotenv
```

## Setup

1. Copy `.env.example` to `.env` and fill in your database connection info:
```bash
cp .env.example .env
# Edit .env with your credentials
```

## Usage

**Download Images from prod database**
1. Replace the variable 'token' with yours (Can get it from https://portal.ihealthunifiedcare.com/care-portal/home -> inspect -> application -> session storage -> https://portal.ihealth-eng.com -> token)
2. Put your .csv file (with column 'FoodLogId') in the folder and change the variable 'csv_path' to your .csv file name (or rename your .csv as foodlog_ai_analysis_img_name.csv)
3. Run "python3 download_images.py"

**Show Images with all the comments from RD and insights from AI**
1. Put your .csv file (with all the required columns) in the folder and change the variable 'csv_path' to your .csv file name (or rename your .csv as foodlog_ai_analysis_img_name.csv)
2. Run "python3 show_foodlog_gallery.py your_data.csv"

**Query Food Logs from Database**
1. Make sure `.env` file is configured with database connection info
2. Run `python3 query_food_logs.py --days 30`
