# AI Tools - Food Log Gallery

**Download Images from prod database**
1. Replace the variable 'token' with yours (Can get it from https://portal.ihealthunifiedcare.com/care-portal/home -> inspect -> application -> session storage -> https://portal.ihealth-eng.com -> token)
2. Put your .csv file (with column 'FoodLogId') in the folder and change the variable 'csv_path' to your .csv file name (or rename your .csv as foodlog_ai_analysis_img_name.csv)
3. Run "python3 download_images.py"

**Show Images with all the comments from RD and insights from AI**
1. Put your .csv file (with ImgName column) in the folder
2. Run "python3 show_foodlog_gallery.py your_data.csv"

**Features:**
- Auto-detects CSV columns (only ImgName required)
- Supports multiple CSV formats
- Self-contained HTML with embedded images
- Smart JSON formatting for RD Comments and Ingredients

**Requirements:**
- Python 3.6+
- pandas
- requests (for image download)
