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
    
    return df


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
    if args.patient_ids:
        patient_ids = [pid.strip() for pid in args.patient_ids.split(",")]
        print(f"[INFO] Using provided patient IDs / 使用提供的病人ID: {len(patient_ids)} patients")
    else:
        print(f"[INFO] Fetching patient IDs from uc_enrolled_programs... / 正在从uc_enrolled_programs获取病人ID...")
        try:
            patient_ids = get_patient_ids_from_enrolled_program(
                client,
                database_name=args.database,
                limit=args.limit_patients
            )
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
            print(f"\n[INFO] Summary / 摘要:")
            print(f"  - Total entries / 总记录数: {len(df)}")
            
            # Check for patient ID column (could be memberId or patient_id)
            # 检查病人ID列（可能是memberId或patient_id）
            patient_col = None
            for col in ["memberId", "patient_id", "member_id"]:
                if col in df.columns:
                    patient_col = col
                    break
            
            if patient_col:
                print(f"  - Unique patients / 唯一病人数: {df[patient_col].nunique()}")
            
            # Check for date columns
            # 检查日期列
            date_col = None
            for col in ["createdAt", "uploadedAt", "created_at", "uploaded_at"]:
                if col in df.columns:
                    date_col = col
                    break
            
            if date_col:
                print(f"  - Date range / 日期范围: {df[date_col].min()} to {df[date_col].max()}")
            
            # Show available columns
            # 显示可用列
            print(f"  - Available columns / 可用列: {', '.join(df.columns.tolist()[:10])}")
            if len(df.columns) > 10:
                print(f"    ... and {len(df.columns) - 10} more / ... 还有 {len(df.columns) - 10} 列")
            
            if not args.no_export:
                output_path = Path(args.output)
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