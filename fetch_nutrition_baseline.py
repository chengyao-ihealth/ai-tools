#!/usr/bin/env python3
"""
Nutrition Baseline API Tool
从CSV文件读取PatientId，调用API获取nutrition baseline数据，并将结果保存回CSV
"""

import requests
import pandas as pd
import json
import time
from typing import Dict, Any
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


def call_nutrition_baseline_api(patient_id: str, api_url: str) -> tuple[Dict[str, Any], float]:
    """
    调用nutrition baseline API
    
    Args:
        patient_id: 患者ID
        api_url: API地址
        
    Returns:
        Tuple[API响应的JSON数据, 请求耗时（秒）]
    """
    payload = {
        "patient_id": patient_id
    }
    
    start_time = time.time()
    try:
        response = requests.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=120
        )
        response.raise_for_status()
        elapsed = time.time() - start_time
        return response.json(), elapsed
    except requests.exceptions.RequestException as e:
        elapsed = time.time() - start_time
        print(f"Error calling API for patient {patient_id}: {e}")
        return {"error": str(e)}, elapsed


def process_csv(input_file: str, output_file: str, api_url: str, delay: float = 0.5, max_workers: int = 1):
    """
    处理CSV文件，为每个PatientId调用API并保存结果
    
    Args:
        input_file: 输入CSV文件路径
        output_file: 输出CSV文件路径
        api_url: API地址
        delay: 请求之间的延迟（秒），仅在max_workers=1时生效
        max_workers: 并行处理的最大线程数（1=顺序处理）
    """
    # 读取CSV文件
    try:
        df = pd.read_csv(input_file)
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        sys.exit(1)
    
    # 检查是否有PatientId列
    if 'PatientId' not in df.columns:
        print("Error: CSV file must have a 'PatientId' column")
        sys.exit(1)
    
    print(f"Found {len(df)} patients in {input_file}")
    print(f"API URL: {api_url}")
    print(f"Processing mode: {'Sequential' if max_workers == 1 else f'Parallel ({max_workers} workers)'}")
    print("-" * 80)
    
    # 用于存储结果（保持顺序）
    results = {}  # {patient_id: (response, elapsed_time)}
    patient_ids = df['PatientId'].tolist()
    
    overall_start_time = time.time()
    
    if max_workers == 1:
        # 顺序处理
        for idx, patient_id in enumerate(patient_ids, 1):
            print(f"[{idx}/{len(patient_ids)}] Processing patient: {patient_id}...", end=" ", flush=True)
            
            response, elapsed = call_nutrition_baseline_api(patient_id, api_url)
            results[patient_id] = (response, elapsed)
            
            # 打印响应摘要
            if "error" in response:
                print(f"❌ Error: {response['error']} ({elapsed:.2f}s)")
            else:
                print(f"✓ Success ({elapsed:.2f}s)")
            
            # 添加延迟避免请求过快
            if idx < len(patient_ids):
                time.sleep(delay)
    else:
        # 并行处理
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            future_to_patient = {
                executor.submit(call_nutrition_baseline_api, patient_id, api_url): patient_id
                for patient_id in patient_ids
            }
            
            # 处理完成的任务
            completed = 0
            for future in as_completed(future_to_patient):
                patient_id = future_to_patient[future]
                completed += 1
                try:
                    response, elapsed = future.result()
                    results[patient_id] = (response, elapsed)
                    
                    if "error" in response:
                        print(f"[{completed}/{len(patient_ids)}] ❌ {patient_id}: Error ({elapsed:.2f}s)")
                    else:
                        print(f"[{completed}/{len(patient_ids)}] ✓ {patient_id}: Success ({elapsed:.2f}s)")
                except Exception as e:
                    print(f"[{completed}/{len(patient_ids)}] ❌ {patient_id}: Exception: {e}")
                    results[patient_id] = ({"error": str(e)}, 0.0)
    
    overall_elapsed = time.time() - overall_start_time
    
    # 按照原始顺序整理结果
    api_responses = []
    request_times = []
    for patient_id in patient_ids:
        response, elapsed = results.get(patient_id, ({"error": "No result"}, 0.0))
        api_responses.append(response)
        request_times.append(elapsed)
    
    # 确定版本号：查找已存在的api_response列
    existing_versions = []
    for col in df.columns:
        if col == 'api_response':
            existing_versions.append(0)  # 无版本号的旧格式
        elif col.startswith('api_response_v'):
            try:
                version = int(col.split('_v')[1])
                existing_versions.append(version)
            except (ValueError, IndexError):
                pass
    
    # 确定新版本号
    if existing_versions:
        next_version = max(existing_versions) + 1
    else:
        next_version = 1
    
    # 生成带版本的列名
    response_col_name = f'api_response_v{next_version}'
    
    # 将API响应添加到DataFrame（新列）
    df[response_col_name] = [json.dumps(resp, ensure_ascii=False) for resp in api_responses]
    
    # 保存到输出文件
    try:
        df.to_csv(output_file, index=False)
        print("-" * 80)
        print(f"✓ Results saved to: {output_file}")
        print(f"  Total columns: {len(df.columns)}")
        print(f"  Added column: {response_col_name}")
        if existing_versions:
            print(f"  Previous versions preserved: {', '.join([f'v{v}' for v in sorted(existing_versions) if v > 0])}")
        
        # 详细的性能统计
        valid_times = [t for t in request_times if t > 0]
        if valid_times:
            print(f"\n⏱️  Performance Summary:")
            print(f"  Total time: {overall_elapsed:.2f}s")
            print(f"  Average request time: {sum(valid_times)/len(valid_times):.2f}s")
            print(f"  Min request time: {min(valid_times):.2f}s")
            print(f"  Max request time: {max(valid_times):.2f}s")
            print(f"  Throughput: {len(patient_ids)/overall_elapsed:.2f} requests/second")
            
            # 找出最慢的请求
            slowest_idx = request_times.index(max(valid_times))
            slowest_patient = patient_ids[slowest_idx]
            print(f"\n  ⚠️  Slowest request: {slowest_patient} ({max(valid_times):.2f}s)")
            
            # 如果并行处理，显示并行效率
            if max_workers > 1:
                theoretical_time = sum(valid_times)
                speedup = theoretical_time / overall_elapsed
                efficiency = speedup / max_workers * 100
                print(f"  Parallel efficiency: {efficiency:.1f}% (speedup: {speedup:.2f}x)")
    except Exception as e:
        print(f"Error saving CSV file: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='调用nutrition baseline API并将结果保存到CSV文件'
    )
    parser.add_argument(
        'input_file',
        nargs='?',
        default='nutrition_baseline_prod_8.csv',
        help='输入CSV文件路径（必须包含PatientId列，默认：nutrition_baseline_dev_5.csv）'
    )
    parser.add_argument(
        '-o', '--output',
        help='输出CSV文件路径（默认：覆盖输入文件）'
    )
    parser.add_argument(
        '-u', '--url',
        default='http://0.0.0.0:8001/nutrition-baseline/quick-generate',
        help='API URL（默认：http://0.0.0.0:8001/nutrition-baseline/quick-generate）'
    )
    parser.add_argument(
        '-d', '--delay',
        type=float,
        default=0.5,
        help='请求之间的延迟秒数（默认：0.5，仅在顺序处理时生效）'
    )
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=1,
        help='并行处理的线程数（默认：1=顺序处理，建议：3-10）'
    )
    
    args = parser.parse_args()
    
    # 如果没有指定输出文件，覆盖输入文件
    if args.output is None:
        args.output = args.input_file
    
    # 处理CSV文件
    process_csv(args.input_file, args.output, args.url, args.delay, args.workers)


if __name__ == '__main__':
    main()

