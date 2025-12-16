#!/usr/bin/env python3
"""
Query Food Logs from Database
从数据库查询食物记录

This script reads patient_id from uc_enrolled_programs collection (MongoDB) 
and queries their food_log based on time range.

这个脚本从uc_enrolled_programs集合（MongoDB）读取patient_id，并根据时间范围查询他们的food_log。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path

import pandas as pd

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ConfigurationError
    from bson import ObjectId
    from dotenv import load_dotenv
except ImportError:
    print("[ERROR] Missing required packages. Please install: pip install pymongo pandas python-dotenv", file=sys.stderr)
    sys.exit(1)

# Load environment variables from .env file
# 从.env文件加载环境变量
load_dotenv()


def get_mongo_client(connection_uri: Optional[str] = None) -> MongoClient:
    """
    Create MongoDB client from connection URI.
    从连接URI创建MongoDB客户端。
    
    Args:
        connection_uri: MongoDB connection URI / MongoDB连接URI
        
    Returns:
        MongoClient: MongoDB client / MongoDB客户端
    """
    # Get from environment variable (loaded from .env file) or use provided
    # 从环境变量（从.env文件加载）获取或使用提供的URI
    uri = connection_uri or os.getenv("MONGO_DATABASE_URI")
    
    if not uri:
        print("[ERROR] MongoDB connection URI not found. Please set MONGO_DATABASE_URI in .env file or as environment variable.", file=sys.stderr)
        print("[ERROR] 未找到MongoDB连接URI。请在.env文件中设置MONGO_DATABASE_URI或作为环境变量。", file=sys.stderr)
        raise ValueError("MongoDB connection URI is required")
    
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        # Test connection
        # 测试连接
        client.admin.command('ping')
        return client
    except (ConnectionFailure, ConfigurationError) as e:
        print(f"[ERROR] Failed to connect to MongoDB / MongoDB连接失败: {e}", file=sys.stderr)
        raise


def get_patient_ids_from_enrolled_program(
    client: MongoClient,
    database_name: str = "UnifiedCare",
    limit: Optional[int] = None
) -> List[str]:
    """
    Get all patient_id from uc_enrolled_programs collection.
    从uc_enrolled_programs集合获取所有patient_id。
    
    Args:
        client: MongoDB client / MongoDB客户端
        database_name: Database name / 数据库名称
        limit: Optional limit on number of patients / 可选的病人数量限制
        
    Returns:
        List[str]: List of patient IDs / 病人ID列表
    """
    db = client[database_name]
    collection = db["uc_enrolled_programs"]
    
    # Try different field names for patient ID
    # 尝试不同的病人ID字段名
    patient_ids = []
    
    # Try patient_id field
    # 尝试 patient_id 字段
    try:
        pipeline = [
            {"$match": {"patient_id": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$patient_id"}},
        ]
        if limit:
            pipeline.append({"$limit": limit})
        results = collection.aggregate(pipeline)
        patient_ids = [doc["_id"] for doc in results if doc["_id"]]
        if patient_ids:
            print(f"[INFO] Found {len(patient_ids)} patients using 'patient_id' field / 使用 'patient_id' 字段找到 {len(patient_ids)} 个病人")
            return patient_ids
    except Exception as e:
        print(f"[WARN] Failed to query using 'patient_id' field / 使用 'patient_id' 字段查询失败: {e}")
    
    # Try _id field (if it's the patient ID)
    # 尝试 _id 字段（如果它是病人ID）
    try:
        pipeline = [
            {"$group": {"_id": "$_id"}},
        ]
        if limit:
            pipeline.append({"$limit": limit})
        results = collection.aggregate(pipeline)
        patient_ids = [str(doc["_id"]) for doc in results if doc["_id"]]
        if patient_ids:
            print(f"[INFO] Found {len(patient_ids)} patients using '_id' field / 使用 '_id' 字段找到 {len(patient_ids)} 个病人")
            return patient_ids
    except Exception as e:
        print(f"[WARN] Failed to query using '_id' field / 使用 '_id' 字段查询失败: {e}")
    
    # Try memberId field
    # 尝试 memberId 字段
    try:
        pipeline = [
            {"$match": {"memberId": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$memberId"}},
        ]
        if limit:
            pipeline.append({"$limit": limit})
        results = collection.aggregate(pipeline)
        patient_ids = [doc["_id"] for doc in results if doc["_id"]]
        if patient_ids:
            print(f"[INFO] Found {len(patient_ids)} patients using 'memberId' field / 使用 'memberId' 字段找到 {len(patient_ids)} 个病人")
            return patient_ids
    except Exception as e:
        print(f"[WARN] Failed to query using 'memberId' field / 使用 'memberId' 字段查询失败: {e}")
    
    return patient_ids


def query_food_logs(
    client: MongoClient,
    patient_ids: List[str],
    database_name: str = "UnifiedCare",
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Query food_log collection for given patient_ids within time range.
    在时间范围内查询给定patient_ids的food_log集合。
    
    Args:
        client: MongoDB client / MongoDB客户端
        patient_ids: List of patient IDs / 病人ID列表
        database_name: Database name / 数据库名称
        start_date: Start date for query / 查询开始日期
        end_date: End date for query / 查询结束日期
        
    Returns:
        pd.DataFrame: Food log data / 食物记录数据
    """
    if not patient_ids:
        return pd.DataFrame()
    
    db = client[database_name]
    collection = db["food_logs"]  # MongoDB collection name
    
    # Print total count of food_logs
    # 打印 food_logs 总数
    try:
        total_count = collection.count_documents({})
        print(f"[INFO] Total food_logs in database / 数据库中food_logs总数: {total_count}")
    except Exception as e:
        print(f"[WARN] Failed to count total food_logs / 统计food_logs总数失败: {e}")
    
    # Build query filter based on API implementation
    # 根据API实现构建查询过滤器
    # API uses memberId field (confirmed from FoodLogRepository.java)
    # API使用memberId字段（从FoodLogRepository.java确认）
    # Convert patient_ids to ObjectId if they are valid ObjectId strings
    # 如果patient_ids是有效的ObjectId字符串，转换为ObjectId
    member_ids = []
    for pid in patient_ids:
        try:
            # Try to convert to ObjectId (MongoDB format)
            # 尝试转换为ObjectId（MongoDB格式）
            member_ids.append(ObjectId(pid))
        except Exception:
            # If conversion fails, use as string
            # 如果转换失败，使用字符串
            member_ids.append(pid)
    
    query_filter: Dict[str, Any] = {
        "memberId": {"$in": member_ids}
    }
    print(f"[INFO] Querying food_logs using 'memberId' field / 使用'memberId'字段查询food_logs")
    print(f"[INFO] Querying for {len(member_ids)} patient IDs / 查询 {len(member_ids)} 个病人ID")
    
    # Add date range filter if provided
    # 如果提供了日期范围，添加日期过滤
    # API uses createdAt field (confirmed from FoodLogRepository.java)
    # API使用createdAt字段（从FoodLogRepository.java确认）
    if start_date or end_date:
        date_filter: Dict[str, Any] = {}
        if start_date:
            date_filter["$gte"] = start_date
        if end_date:
            date_filter["$lte"] = end_date
        
        # Add createdAt filter (as per API implementation)
        # 添加createdAt过滤（根据API实现）
        query_filter["createdAt"] = date_filter
    
    # Query and convert to list
    # 查询并转换为列表
    # Sort by createdAt descending (as per API implementation)
    # 按createdAt降序排序（根据API实现）
    try:
        # First, count matching documents
        # 首先，统计匹配的文档数
        match_count = collection.count_documents(query_filter)
        print(f"[INFO] Found {match_count} matching food_logs / 找到 {match_count} 条匹配的food_logs")
        
        # Query with sort (same as API: Sort.by(Sort.Direction.DESC, "createdAt"))
        # 查询并排序（与API相同：按createdAt降序）
        cursor = collection.find(query_filter).sort("createdAt", -1)
        documents = list(cursor)
        
        if not documents:
            print(f"[WARN] No documents returned from query / 查询未返回文档")
            # Show a sample of patient_ids to help debug
            # 显示部分patient_ids以帮助调试
            if len(patient_ids) > 0:
                print(f"[INFO] Sample patient IDs being queried / 查询的示例病人ID: {patient_ids[:5]}")
            return pd.DataFrame()
    except Exception as e:
        print(f"[ERROR] Failed to query food_logs / 查询food_logs失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return pd.DataFrame()
    
    # Convert to DataFrame
    # 转换为DataFrame
    df = pd.DataFrame(documents)
    
    # Convert _id to string if present (for CSV compatibility)
    # 如果存在，将_id转换为字符串（以便CSV兼容）
    if "_id" in df.columns:
        df["_id"] = df["_id"].astype(str)
    
    # Filter out records with empty images (invalid food logs)
    # 过滤掉images为空的记录（无效的food log）
    if "images" in df.columns:
        initial_count = len(df)
        # Filter: keep only records where images is not null/None and not empty list
        # 过滤：只保留images不为null/None且不为空列表的记录
        def has_valid_images(x):
            try:
                # Check for None
                # 检查None
                if x is None:
                    return False
                # Check for NaN (scalar values only)
                # 检查NaN（仅标量值）
                try:
                    if pd.isna(x):
                        return False
                except (TypeError, ValueError):
                    # pd.isna() may fail for list types, which is fine
                    # pd.isna()对于列表类型可能会失败，这是正常的
                    pass
                # Check if it's a non-empty list
                # 检查是否是非空列表
                if isinstance(x, list):
                    return len(x) > 0
                return False
            except (TypeError, ValueError):
                return False
        
        df = df[df["images"].apply(has_valid_images)]
        filtered_count = initial_count - len(df)
        if filtered_count > 0:
            print(f"[INFO] Filtered out {filtered_count} records with empty images / 过滤掉 {filtered_count} 条images为空的记录")
    elif len(df) > 0:
        # If images column doesn't exist, warn but don't filter
        # 如果images列不存在，警告但不过滤
        print(f"[WARN] 'images' column not found in food_logs / 在food_logs中未找到'images'列")
    
    return df


def query_patient_all_logs(
    client: MongoClient,
    patient_id: str,
    database_name: str = "UnifiedCare",
) -> Dict[str, Any]:
    """
    Query all food logs for a single patient (no date range limit).
    查询单个病人的所有food log（不限制日期范围）。
    
    Args:
        client: MongoDB client / MongoDB客户端
        patient_id: Patient ID / 病人ID
        database_name: Database name / 数据库名称
        
    Returns:
        Dict with total_logs, first_log_date, last_log_date / 
        包含total_logs, first_log_date, last_log_date的字典
    """
    db = client[database_name]
    collection = db["food_logs"]
    
    # Convert patient_id to ObjectId if valid
    # 如果有效，将patient_id转换为ObjectId
    try:
        member_id = ObjectId(patient_id)
    except Exception:
        member_id = patient_id
    
    query_filter = {
        "memberId": member_id,
        "images": {"$exists": True, "$ne": None}
    }
    
    try:
        # Use aggregation to count and get date range
        # 使用聚合管道统计并获取日期范围
        pipeline = [
            {"$match": query_filter},
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
            {"$match": {"has_images": True}},
            {
                "$group": {
                    "_id": None,
                    "total_logs": {"$sum": 1},
                    "first_log_date": {"$min": "$createdAt"},
                    "last_log_date": {"$max": "$createdAt"}
                }
            }
        ]
        
        result = list(collection.aggregate(pipeline))
        if result and len(result) > 0:
            return {
                "total_logs": result[0].get("total_logs", 0),
                "first_log_date": result[0].get("first_log_date"),
                "last_log_date": result[0].get("last_log_date")
            }
        return {
            "total_logs": 0,
            "first_log_date": None,
            "last_log_date": None
        }
    except Exception as e:
        print(f"[WARN] Failed to query all logs for patient {patient_id} / 查询病人 {patient_id} 的全部log失败: {e}")
        return {
            "total_logs": 0,
            "first_log_date": None,
            "last_log_date": None
        }


def count_total_valid_food_logs(
    client: Any,
    database_name: str = "UnifiedCare",
) -> int:
    """
    Count total valid food logs (with non-empty images) in database.
    统计数据库中所有有效的food log总数（images不为空的）。
    
    Args:
        client: MongoDB client / MongoDB客户端
        database_name: Database name / 数据库名称
        
    Returns:
        int: Total count of valid food logs / 有效food log总数
    """
    db = client[database_name]
    collection = db["food_logs"]
    
    try:
        # Count documents where images exists and is not empty
        # 统计images存在且不为空的文档数
        # Using aggregation to check for non-empty images array
        # 使用聚合管道检查非空images数组
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
                "$count": "total"
            }
        ]
        
        result = list(collection.aggregate(pipeline))
        if result and len(result) > 0:
            return result[0].get("total", 0)
        return 0
    except Exception as e:
        print(f"[WARN] Failed to count total valid food_logs / 统计总有效food_logs失败: {e}")
        return 0


def main():
    """
    Main function to query food logs from database.
    从数据库查询食物记录的主函数。
    """
    parser = argparse.ArgumentParser(
        description="Query food logs from MongoDB for patients in uc_enrolled_programs. / 从MongoDB查询uc_enrolled_programs中病人的食物记录。"
    )
    
    # Database connection arguments
    # 数据库连接参数
    parser.add_argument(
        "--mongo-uri",
        default=None,
        help="MongoDB connection URI (or set MONGO_DATABASE_URI env var) / MongoDB连接URI（或设置MONGO_DATABASE_URI环境变量）"
    )
    parser.add_argument(
        "--database",
        default="UnifiedCare",
        help="Database name / 数据库名称"
    )
    
    # Query arguments
    # 查询参数
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD) / 开始日期（YYYY-MM-DD）"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date (YYYY-MM-DD) / 结束日期（YYYY-MM-DD）"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days back from today / 从今天往前多少天"
    )
    parser.add_argument(
        "--limit-patients",
        type=int,
        default=None,
        help="Limit number of patients to query / 限制查询的病人数量"
    )
    parser.add_argument(
        "--patient-ids",
        type=str,
        default=None,
        help="Comma-separated list of specific patient IDs / 逗号分隔的特定病人ID列表"
    )
    
    # Output arguments
    # 输出参数
    parser.add_argument(
        "--output",
        default="food_logs.csv",
        help="Output CSV file path / 输出CSV文件路径"
    )
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Don't export to CSV, just print summary / 不导出CSV，只打印摘要"
    )
    
    args = parser.parse_args()
    
    # Connect to MongoDB
    # 连接到MongoDB
    print(f"[INFO] Connecting to MongoDB... / 正在连接MongoDB...")
    try:
        client = get_mongo_client(args.mongo_uri)
        print(f"[OK] MongoDB connection successful / MongoDB连接成功")
    except Exception as e:
        print(f"[ERROR] Failed to connect to MongoDB / MongoDB连接失败: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Get patient IDs
    # 获取病人ID
    total_enrolled_patients = 0
    if args.patient_ids:
        patient_ids = [pid.strip() for pid in args.patient_ids.split(",")]
        total_enrolled_patients = len(patient_ids)
        print(f"[INFO] Using provided patient IDs / 使用提供的病人ID: {len(patient_ids)} patients")
    else:
        print(f"[INFO] Fetching patient IDs from uc_enrolled_programs... / 正在从uc_enrolled_programs获取病人ID...")
        try:
            patient_ids = get_patient_ids_from_enrolled_program(
                client,
                database_name=args.database,
                limit=args.limit_patients
            )
            total_enrolled_patients = len(patient_ids)
            print(f"[OK] Found {len(patient_ids)} patients / 找到 {len(patient_ids)} 个病人")
        except Exception as e:
            print(f"[ERROR] Failed to fetch patient IDs / 获取病人ID失败: {e}", file=sys.stderr)
            client.close()
            sys.exit(1)
    
    if not patient_ids:
        print("[WARN] No patient IDs found / 未找到病人ID", file=sys.stderr)
        client.close()
        sys.exit(0)
    
    # Parse date range
    # 解析日期范围
    start_date = None
    end_date = None
    
    if args.days:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=args.days)
        print(f"[INFO] Querying last {args.days} days / 查询最近 {args.days} 天")
    else:
        if args.start_date:
            start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
        if args.end_date:
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
            # Set to end of day
            # 设置为当天结束时间
            end_date = end_date.replace(hour=23, minute=59, second=59)
        
        if start_date or end_date:
            print(f"[INFO] Date range / 日期范围: {start_date or 'N/A'} to {end_date or 'N/A'}")
    
    # Query food logs
    # 查询食物记录
    print(f"[INFO] Querying food logs for {len(patient_ids)} patients... / 正在查询 {len(patient_ids)} 个病人的食物记录...")
    try:
        df = query_food_logs(
            client,
            patient_ids,
            database_name=args.database,
            start_date=start_date,
            end_date=end_date
        )
        print(f"[OK] Found {len(df)} food log entries / 找到 {len(df)} 条食物记录")
        
        if len(df) > 0:
            # Count total valid food logs in database
            # 统计数据库中所有有效的food log总数
            total_valid_food_logs = count_total_valid_food_logs(client, database_name=args.database)
            
            # Check for patient ID column (could be memberId or patient_id)
            # 检查病人ID列（可能是memberId或patient_id）
            patient_col = None
            for col in ["memberId", "patient_id", "member_id"]:
                if col in df.columns:
                    patient_col = col
                    break
            
            unique_patients_in_query = 0
            if patient_col:
                unique_patients_in_query = df[patient_col].nunique()
            
            # Check for date columns
            # 检查日期列
            date_col = None
            for col in ["createdAt", "uploadedAt", "created_at", "uploaded_at"]:
                if col in df.columns:
                    date_col = col
                    break
            
            # Calculate number of days and statistics for summary
            # 计算天数和统计信息用于摘要
            num_days = 1
            patients_avg_gt_1 = 0
            patients_logging_every_day = 0
            
            if patient_col:
                # Calculate number of days in the date range
                # 计算日期范围内的天数
                if args.days:
                    # If --days parameter was used, use it directly
                    # 如果使用了--days参数，直接使用它
                    num_days = args.days
                elif start_date and end_date:
                    # Use the actual date range from query parameters
                    # 使用查询参数中的实际日期范围
                    # Normalize dates to date level (ignore time) for accurate day count
                    # 将日期归一化到日期级别（忽略时间）以准确计算天数
                    try:
                        start_dt = pd.to_datetime(start_date)
                        end_dt = pd.to_datetime(end_date)
                        start_date_only = start_dt.date()
                        end_date_only = end_dt.date()
                        num_days = (end_date_only - start_date_only).days + 1
                    except Exception:
                        # Fallback to simple calculation
                        # 回退到简单计算
                        num_days = (end_date - start_date).days + 1
                elif date_col:
                    # Calculate from actual data
                    # 从实际数据计算
                    try:
                        date_series = pd.to_datetime(df[date_col], errors='coerce')
                        min_date = date_series.min()
                        max_date = date_series.max()
                        if pd.notna(min_date) and pd.notna(max_date):
                            # Normalize to date level
                            # 归一化到日期级别
                            min_date_only = min_date.date()
                            max_date_only = max_date.date()
                            num_days = (max_date_only - min_date_only).days + 1
                    except Exception:
                        pass
                
                # Group by patient and count logs
                # 按病人分组并统计log数
                patient_log_counts = df.groupby(patient_col).size().reset_index(name='total_logs')
                patient_log_counts = patient_log_counts.sort_values('total_logs', ascending=False)
                
                # Calculate statistics: patients with avg logs > 1 and patients logging every day
                # 计算统计：平均log数>1的病人数和每天都log的病人数
                if num_days > 0 and date_col:
                    # Calculate average logs per day for each patient
                    # 计算每个病人每天的平均log数
                    patient_log_counts['avg_logs_per_day'] = patient_log_counts['total_logs'] / num_days
                    
                    # Count patients with average logs > 1
                    # 统计平均log数>1的病人数
                    patients_avg_gt_1 = len(patient_log_counts[patient_log_counts['avg_logs_per_day'] > 1])
                    
                    # Count patients who log every day
                    # 统计每天都log的病人数（在日期范围内每天都有至少一个log）
                    try:
                        # Convert date column to datetime and extract date part
                        # 将日期列转换为datetime并提取日期部分
                        df_with_dates = df.copy()
                        df_with_dates['log_date'] = pd.to_datetime(df_with_dates[date_col], errors='coerce').dt.date
                        
                        # For each patient, count unique dates with logs
                        # 对每个病人，统计有log的唯一日期数
                        patient_unique_dates = df_with_dates.groupby(patient_col)['log_date'].nunique().reset_index(name='unique_dates')
                        
                        # Merge with patient_log_counts
                        # 与patient_log_counts合并
                        patient_stats = patient_log_counts.merge(patient_unique_dates, on=patient_col, how='left')
                        patient_stats['unique_dates'] = patient_stats['unique_dates'].fillna(0)
                        
                        # Patients who log every day: unique_dates >= num_days
                        # 每天都log的病人：唯一日期数 >= 天数
                        patients_logging_every_day = len(patient_stats[patient_stats['unique_dates'] >= num_days])
                    except Exception as e:
                        # Fallback: use total_logs >= num_days as approximation
                        # 回退：使用总log数 >= 天数作为近似
                        patients_logging_every_day = len(patient_log_counts[patient_log_counts['total_logs'] >= num_days])
                        print(f"[WARN] Failed to calculate exact daily logging count, using approximation / 计算精确每日log数失败，使用近似值: {e}")
            
            print(f"\n[INFO] Summary / 摘要:")
            print(f"  - Valid food logs / 有效食物记录数: {len(df)}")
            print(f"  - Patients with food logs / 有食物记录的病人数: {unique_patients_in_query}")
            print(f"  - Total valid food logs / 总有效食物记录数: {total_valid_food_logs}")
            print(f"  - Total enrolled patients / 总注册病人数: {total_enrolled_patients}")
            if date_col:
                print(f"  - Date range / 日期范围: {df[date_col].min()} to {df[date_col].max()}")
            if patient_col and num_days > 0:
                print(f"  - Patients with avg logs > 1 / 平均log数>1的病人数: {patients_avg_gt_1}")
                print(f"  - Patients logging every day / 每天都log的病人数: {patients_logging_every_day}")
            
            # Calculate top 10 patients by food log count
            # 计算food log最多的前10个病人
            if patient_col:
                
                # Get top 10 patients
                # 获取前10个病人
                top_10_patients = patient_log_counts.head(10)
                
                if len(top_10_patients) > 0:
                    print(f"\n[INFO] Top 10 Patients by Food Log Count / food log最多的前10个病人:")
                    print(f"  (Date range: {num_days} days / 日期范围: {num_days} 天)")
                    for rank, (idx, row) in enumerate(top_10_patients.iterrows(), start=1):
                        patient_id = str(row[patient_col])
                        logs_in_range = int(row['total_logs'])  # Logs within date range
                        avg_logs_per_day = logs_in_range / num_days if num_days > 0 else 0
                        
                        # Query all historical logs for this patient
                        # 查询该病人的所有历史log
                        all_logs_info = query_patient_all_logs(
                            client,
                            patient_id,
                            database_name=args.database
                        )
                        total_all_logs = all_logs_info["total_logs"]
                        first_log_date = all_logs_info["first_log_date"]
                        last_log_date = all_logs_info["last_log_date"]
                        
                        print(f"  {rank}. Patient ID: {patient_id}")
                        
                        # Format dates for display (only date part, no time)
                        # 格式化日期显示（只显示日期部分，不显示时间）
                        first_date_str = ""
                        last_date_str = ""
                        if first_log_date:
                            if isinstance(first_log_date, datetime):
                                first_date_str = first_log_date.strftime("%Y-%m-%d")
                            else:
                                try:
                                    first_date_str = pd.to_datetime(first_log_date).strftime("%Y-%m-%d")
                                except:
                                    first_date_str = str(first_log_date)
                        if last_log_date:
                            if isinstance(last_log_date, datetime):
                                last_date_str = last_log_date.strftime("%Y-%m-%d")
                            else:
                                try:
                                    last_date_str = pd.to_datetime(last_log_date).strftime("%Y-%m-%d")
                                except:
                                    last_date_str = str(last_log_date)
                        
                        # Format total logs line with date range
                        # 格式化总log数行，包含日期范围
                        if first_date_str and last_date_str:
                            print(f"     - Total logs (all time) / 从开始log起的总log数: {total_all_logs}, from {first_date_str} to {last_date_str}")
                        else:
                            print(f"     - Total logs (all time) / 从开始log起的总log数: {total_all_logs}")
                        
                        print(f"     - Logs in date range / 日期范围内log数: {logs_in_range}")
                        print(f"     - Average logs per day (in range) / 每天平均log数（日期范围内）: {avg_logs_per_day:.2f}")
            
            if not args.no_export:
                output_path = Path(args.output)
                # Reorder columns: images should be in the 3rd position (index 2)
                # 重新排列列：images应该在第三位（索引2）
                if "images" in df.columns:
                    cols = df.columns.tolist()
                    # Remove images from current position
                    # 从当前位置移除images
                    cols.remove("images")
                    # Insert images at 3rd position (index 2)
                    # 将images插入到第三位（索引2）
                    if len(cols) >= 2:
                        cols.insert(2, "images")
                    else:
                        # If less than 2 columns, just append
                        # 如果少于2列，直接追加
                        cols.append("images")
                    df = df[cols]
                df.to_csv(output_path, index=False, encoding="utf-8")
                print(f"\n[OK] Exported to / 已导出到: {output_path.resolve()}")
        else:
            print("[WARN] No food logs found for the given criteria / 未找到符合条件的食物记录")
            
    except Exception as e:
        print(f"[ERROR] Failed to query food logs / 查询食物记录失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()