#!/usr/bin/env python3
"""
生成带完整样式的动态营养基线报告对比HTML页面
- 彩虹色进度条
- 绿框高亮新功能
- 动态渲染所有字段
- 包含HIPAA合规声明
"""

import pandas as pd
import json
import sys
import argparse


def normalize_field_name(field_name):
    """规范化字段名用于比较"""
    import re
    normalized = field_name.lower()
    # 移除开头的数字编号（如 "1. ", "10. "）
    normalized = re.sub(r'^\d+\.\s*', '', normalized)
    # 替换所有非字母数字字符为下划线
    normalized = re.sub(r'[^a-z0-9]+', '_', normalized)
    # 移除首尾下划线
    normalized = normalized.strip('_')
    return normalized


def detect_new_fields_for_patient(patient_data, versions):
    """检测每个版本中相对于前一版本的新字段"""
    new_fields_map = {}
    
    for idx, version in enumerate(versions):
        new_fields_map[version] = []
        
        # 第一个版本没有新字段
        if idx == 0:
            continue
        
        current_data = patient_data['versions'].get(version)
        if not current_data:
            continue
        
        current_report = current_data.get('baseline_report', current_data)
        
        # 获取前一个版本
        prev_version = versions[idx - 1]
        prev_data = patient_data['versions'].get(prev_version)
        if not prev_data:
            continue
        
        prev_report = prev_data.get('baseline_report', prev_data)
        
        # 规范化前一版本的字段名
        prev_normalized_fields = set()
        for key in prev_report.keys():
            if prev_report[key] is not None and prev_report[key] != '':
                prev_normalized_fields.add(normalize_field_name(key))
        
        # 检查当前版本的新字段
        for key in current_report.keys():
            if current_report[key] is not None and current_report[key] != '':
                normalized_key = normalize_field_name(key)
                if normalized_key not in prev_normalized_fields:
                    new_fields_map[version].append(key)
    
    return new_fields_map


# HIPAA数据匿名化功能已移除，保持原始数据完整性

def read_csv_versions(csv_file):
    """读取CSV文件并提取所有版本的数据"""
    df = pd.read_csv(csv_file)
    
    # 查找所有 api_response 列
    api_response_cols = [col for col in df.columns if col.startswith('api_response')]
    
    if not api_response_cols:
        print("Error: No api_response columns found in CSV")
        return {}
    
    # 按版本号排序
    api_response_cols.sort()
    
    # 提取版本号和描述
    versions = []
    version_descriptions = {}
    for col in api_response_cols:
        if col == 'api_response':
            versions.append('v0')
            version_descriptions['v0'] = ''
        else:
            # 提取版本号和括号内的描述
            version_part = col.replace('api_response_', '')
            
            # 检查是否有括号内容
            if '(' in version_part and ')' in version_part:
                version = version_part.split('(')[0]  # 提取括号前的版本号
                description = version_part.split('(')[1].rstrip(')')  # 提取括号内的描述
                version_descriptions[version] = description
            else:
                version = version_part
                version_descriptions[version] = ''
            
            versions.append(version)
    
    print(f"Found {len(versions)} versions: {', '.join(api_response_cols)}")
    
    # 读取每个患者的所有版本数据
    patients_data = []
    for idx, row in df.iterrows():
        patient_data = {
            'patient_id': row['PatientId'],  # 保持原始患者ID
            'versions': {}
        }
        
        for col, version in zip(api_response_cols, versions):
            if pd.notna(row[col]):
                try:
                    data = json.loads(row[col])
                    # 保持原始数据，不进行匿名化
                    patient_data['versions'][version] = data
                except json.JSONDecodeError as e:
                    print(f"Warning: Failed to parse {col} for patient {idx+1}: {e}")
                    continue
        
        if patient_data['versions']:
            # 为每个患者检测新字段
            patient_data['new_fields'] = detect_new_fields_for_patient(patient_data, versions)
            patients_data.append(patient_data)
    
    return {'patients': patients_data, 'versions': versions, 'version_descriptions': version_descriptions}


def generate_html(data, output_file, input_file):
    """生成HTML文件"""
    
    patients = data['patients']
    versions = data['versions']
    version_descriptions = data['version_descriptions']
    
    if not patients:
        print("Error: No patient data available")
        return
    
    print(f"Generating comparison for versions: {', '.join(versions)}")
    print(f"Using patient: {patients[0]['patient_id']}")
    
    # 生成 HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nutrition Baseline Report Comparison (HIPAA Compliant)</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
            background: #f5f1e8;
        }}
        
        .report-column {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 2px 8px rgba(139, 115, 85, 0.1);
            width: 450px;
            flex-shrink: 0;
        }}
        
        .version-header {{
            background: linear-gradient(135deg, #d4b896 0%, #c9a97a 100%);
            color: white;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 20px;
            box-shadow: 0 4px 12px rgba(139, 115, 85, 0.2);
        }}
        
        .section-card {{
            background: #fefdfb;
            border: 1px solid #e8dcc8;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 16px;
            transition: all 0.3s;
        }}
        
        .section-card:hover {{
            box-shadow: 0 4px 12px rgba(139, 115, 85, 0.15);
            border-color: #d4b896;
        }}
        
        .section-title {{
            font-size: 1rem;
            font-weight: 700;
            color: #1f2937;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 2px solid #e8dcc8;
        }}
        
        .new-badge {{
            background: linear-gradient(135deg, #fbbf24 0%, #f59e0b 100%);
            color: white;
            padding: 2px 8px;
            border-radius: 6px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
            margin-left: 8px;
        }}
        
        .new-feature {{
            border: 2px solid #10b981 !important;
            position: relative;
        }}
        
        .confidence-bar {{
            height: 8px;
            background: linear-gradient(90deg, #ef4444 0%, #f59e0b 50%, #10b981 100%);
            border-radius: 999px;
            overflow: hidden;
            position: relative;
        }}
        
        .confidence-fill {{
            height: 100%;
            background: #e8dcc8;
            border-radius: 999px;
            transition: width 0.6s;
            position: absolute;
            top: 0;
            right: 0;
        }}
        
        .priority-badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 0.75rem;
            font-weight: 600;
        }}
        
        .priority-high {{
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #f87171;
        }}
        
        .priority-medium {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fbbf24;
        }}
        
        .priority-low {{
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #34d399;
        }}
        
        .recommendation-high {{
            background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%);
            border-left: 4px solid #ef4444;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 12px;
        }}
        
        .recommendation-medium {{
            background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
            border-left: 4px solid #f59e0b;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 12px;
        }}
        
        .recommendation-low {{
            background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
            border-left: 4px solid #10b981;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 12px;
        }}
        
        .clinical-highlight {{
            background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%);
            border-left: 4px solid #ef4444;
        }}
        
        .json-key {{
            color: #1f2937;
            font-weight: 600;
            font-size: 1rem;
        }}
        
        .json-string {{
            color: #1f2937;
            line-height: 1.6;
        }}
        
        .prose p {{
            color: #1f2937;
            line-height: 1.6;
        }}
        
        .text-beige {{
            color: #1f2937;
        }}
        
        .bg-beige-light {{
            background: #f9f6f0;
        }}
        
        .border-beige {{
            border-color: #d4b896;
        }}
        
        .json-number {{
            color: #0891b2;
            font-weight: 600;
        }}
        
        .json-array-item {{
            background: #f9f6f0;
            border-left: 3px solid #a89373;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 8px;
        }}
        
        .json-object {{
            background: #f9f6f0;
            padding: 12px;
            border-radius: 6px;
            margin-top: 8px;
        }}
        
        .habit-item {{
            background: #f9f6f0;
            border-left: 3px solid #a89373;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 8px;
        }}
        
        .improvement-item {{
            background: #fef9f3;
            border-left: 3px solid #d4a574;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 8px;
        }}
        
        .pattern-item {{
            background: #fefdfb;
            border: 1px solid #e8dcc8;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 12px;
        }}
        
        .prompt-section {{
            background: #f9f6f0;
            border: 1px solid #e8dcc8;
            border-radius: 8px;
            padding: 16px;
            margin-top: 20px;
        }}
        
        .prompt-content {{
            font-size: 0.75rem;
            color: #374151;
            line-height: 1.6;
            max-height: 400px;
            overflow-y: auto;
            white-space: pre-wrap;
            font-family: 'Courier New', monospace;
            background: white;
            padding: 12px;
            border-radius: 4px;
            border: 1px solid #e5e7eb;
        }}
        
        .comparison-grid {{
            display: flex;
            gap: 24px;
            overflow-x: auto;
            padding-bottom: 20px;
        }}
        
        @media (max-width: 768px) {{
            .comparison-grid {{
                flex-direction: column;
            }}
            
            .report-column {{
                width: auto;
            }}
        }}
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="container mx-auto">
        <!-- HIPAA Compliance Notice -->
        <div class="bg-blue-50 border-l-4 border-blue-400 p-4 mb-6 rounded-r-lg">
            <div class="flex">
                <div class="flex-shrink-0">
                    <svg class="h-5 w-5 text-blue-400" viewBox="0 0 20 20" fill="currentColor">
                        <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
                    </svg>
                </div>
                <div class="ml-3">
                    <p class="text-sm text-blue-700">
                        <strong>🔒 HIPAA Notice:</strong> This report contains patient health information. Access is restricted to authorized healthcare personnel only. All data should be handled according to HIPAA privacy and security standards.
                    </p>
                </div>
            </div>
        </div>
        
        <!-- Header -->
        <div class="bg-white rounded-xl shadow-sm p-8 mb-6">
            <h1 class="text-3xl font-bold mb-2" style="color: #1f2937;">
                Nutrition Baseline Report Comparison
            </h1>
            <p class="text-gray-600 mb-1">Compare AI-generated nutrition baseline assessment</p>
            <p class="text-gray-600">比较 AI 生成的营养基线评估报告的不同迭代版本</p>
            
            <!-- Patient Selector -->
            <div class="mt-6 pt-6 border-t" style="border-color: #d4b896;">
                <label class="text-sm font-semibold block mb-2" style="color: #1f2937;">Select Patient:</label>
                <select id="patient-selector" class="w-full md:w-auto px-4 py-2 border rounded-lg text-sm" style="border-color: #d4b896; min-width: 200px; max-width: 100%;">
                </select>
            </div>
        </div>

        <!-- Comparison Grid -->
        <div class="comparison-grid" id="comparison-grid"></div>
    </div>

    <script>
        const patientsData = {json.dumps(patients, ensure_ascii=False)};
        const versions = {json.dumps(versions)};
        const versionDescriptions = {json.dumps(version_descriptions)};

        // 字段显示配置
        const fieldConfig = {{
            'summary': {{ icon: '📋', title: 'Executive Summary' }},
            'executive_summary': {{ icon: '📋', title: 'Executive Summary' }},
            'executivesummary': {{ icon: '📋', title: 'Executive Summary' }},
            'data_coverage': {{ icon: '📊', title: 'Data Coverage' }},
            'ai_assessment_metadata': {{ icon: '🤖', title: 'AI Assessment Metadata' }},
            'positive_habits': {{ icon: '✅', title: 'Positive Habits' }},
            'improvement_areas': {{ icon: '🎯', title: 'Areas for Improvement' }},
            'glucose_correlations': {{ icon: '🩸', title: 'Glucose Correlations' }},
            'glucose_blood_pressure_correlations': {{ icon: '🩸', title: 'Glucose/Blood Pressure Correlations' }},
            'pattern_statistics': {{ icon: '📈', title: 'Pattern Statistics' }},
            'meal_pattern_labels': {{ icon: '🏷️', title: 'Meal Pattern Labels' }},
            'macronutrient_summary': {{ icon: '🍽️', title: 'Macronutrient Summary' }},
            'macronutrient_analysis': {{ icon: '🍽️', title: 'Macronutrient Analysis' }},
            'meal_timing_patterns': {{ icon: '⏰', title: 'Meal Timing Patterns' }},
            'meal_timing_and_patterns': {{ icon: '⏰', title: 'Meal Timing and Patterns' }},
            'food_choices_analysis': {{ icon: '🥗', title: 'Food Choices Analysis' }},
            'key_nutrition_patterns': {{ icon: '📈', title: 'Key Nutrition Patterns' }},
            'lab_correlations': {{ icon: '🔬', title: 'Lab Correlations' }},
            'recommendations': {{ icon: '💡', title: 'Recommendations' }},
            'prioritized_recommendations': {{ icon: '💡', title: 'Prioritized Recommendations' }},
            'narrative_summary': {{ icon: '📝', title: 'Narrative Summary' }},
            'coaching_takeaway': {{ icon: '🎯', title: 'Coaching Takeaway' }},
            'clinical_considerations': {{ icon: '⚕️', title: 'Clinical Considerations' }}
        }};

        // 获取新字段（从Python端预计算的数据）
        function getNewFields(patientIndex) {{
            return patientsData[patientIndex].new_fields || {{}};
        }}

        // 检查值是否为空
        function isEmpty(value) {{
            if (value === null || value === undefined) return true;
            if (typeof value === 'string') return value.trim() === '';
            if (Array.isArray(value)) return value.length === 0;
            if (typeof value === 'object') return Object.keys(value).length === 0;
            return false;
        }}

        // 获取字段显示名称
        function getFieldDisplay(key) {{
            // 规范化字段名用于查找配置
            const normalizedKey = key.toLowerCase()
                .replace(/^\\d+\\.\\s*/, '')  // 移除开头的数字编号
                .replace(/[^a-z0-9]+/g, '_')  // 替换特殊字符为下划线
                .replace(/^_+|_+$/g, '');     // 移除首尾下划线
            
            if (fieldConfig[normalizedKey]) {{
                return `${{fieldConfig[normalizedKey].icon}} ${{fieldConfig[normalizedKey].title}}`;
            }}
            
            // 如果没有配置，使用原始字段名进行处理
            return key
                .replace(/^\\d+\\.\\s*/, '')  // 移除开头的数字编号
                .replace(/_/g, ' ')
                .replace(/([A-Z])/g, ' $1')
                .split(' ')
                .map(word => word.charAt(0).toUpperCase() + word.slice(1))
                .join(' ')
                .trim();
        }}

        // 特殊渲染 AI Assessment Metadata（带彩虹进度条）
        function renderAIMetadata(meta) {{
            if (!meta || !meta.overall_confidence) return renderGenericObject(meta);
            
            const confidence = meta.overall_confidence;
            const confidenceColor = confidence < 0.5 ? '#ef4444' : confidence < 0.8 ? '#f59e0b' : '#10b981';
            
            return `
                <div style="margin-top: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <span class="json-key">Overall Confidence</span>
                        <span style="font-size: 1.125rem; font-weight: bold; color: ${{confidenceColor}}">
                            ${{(confidence * 100).toFixed(0)}}%
                        </span>
                    </div>
                    <div class="confidence-bar">
                        <div class="confidence-fill" style="width: ${{100 - confidence * 100}}%"></div>
                    </div>
                </div>
                ${{meta.assessment_rationale ? `
                    <div class="json-object" style="margin-top: 12px;">
                        <div class="json-key" style="margin-bottom: 4px;">Assessment Rationale</div>
                        <div class="json-string" style="font-size: 0.875rem;">${{meta.assessment_rationale}}</div>
                    </div>
                ` : ''}}
                ${{meta.data_completeness ? `
                    <div class="json-object" style="margin-top: 8px;">
                        <div class="json-key" style="margin-bottom: 4px;">Data Completeness</div>
                        <div class="json-string" style="font-size: 0.875rem;">${{meta.data_completeness}}</div>
                    </div>
                ` : ''}}
                ${{meta.primary_factors && meta.primary_factors.length > 0 ? `
                    <div class="json-object" style="margin-top: 8px;">
                        <div class="json-key" style="margin-bottom: 4px;">Primary Factors</div>
                        <ul style="margin-top: 4px; padding-left: 20px;">
                            ${{meta.primary_factors.map(f => `<li class="json-string" style="font-size: 0.875rem; margin-bottom: 2px;">${{f}}</li>`).join('')}}
                        </ul>
                    </div>
                ` : ''}}
            `;
        }}

        // 特殊渲染 Recommendations
        function renderRecommendations(recommendations) {{
            if (!Array.isArray(recommendations)) return '';
            
            return recommendations.map(rec => {{
                if (!rec.category && !rec.recommendation) return renderGenericObject(rec);
                
                const priority = rec.priority || 'Medium';
                const priorityClass = `priority-${{priority.toLowerCase()}}`;
                const bgClass = `recommendation-${{priority.toLowerCase()}}`;
                
                return `
                    <div class="${{bgClass}}">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <div style="font-size: 0.875rem; color: #1f2937; font-weight: bold;">${{rec.category || 'Recommendation'}}</div>
                            <span class="priority-badge ${{priorityClass}}">${{priority}}</span>
                        </div>
                        ${{rec.recommendation ? `<div style="font-size: 0.875rem; color: #374151; margin-bottom: 4px;">${{rec.recommendation}}</div>` : ''}}
                        ${{rec.rationale ? `<div style="font-size: 0.75rem; font-style: italic; color: #6b7280;">${{rec.rationale}}</div>` : ''}}
                    </div>
                `;
            }}).join('');
        }}

        // 渲染数组项
        function renderArrayItem(item, key) {{
            if (typeof item === 'string') {{
                const className = key === 'positive_habits' ? 'habit-item' : 
                                 key === 'improvement_areas' ? 'improvement-item' :
                                 key === 'key_nutrition_patterns' ? 'pattern-item' :
                                 'json-array-item';
                return `<div class="${{className}}">${{item}}</div>`;
            }}
            
            if (typeof item === 'object') {{
                // 检查常见的结构模式
                const mainField = item.habit || item.area || item.pattern || item.name || item.label;
                
                if (mainField) {{
                    const className = item.habit ? 'habit-item' : 
                                     item.area ? 'improvement-item' :
                                     item.pattern ? 'pattern-item' :
                                     'json-array-item';
                    
                    let html = `<div class="${{className}}">`;
                    html += `<div style="font-weight: 600; margin-bottom: 4px; color: #1f2937;">${{mainField}}</div>`;
                    
                    // 添加其他字段
                    Object.keys(item).forEach(k => {{
                        if (k !== 'habit' && k !== 'area' && k !== 'pattern' && k !== 'name' && k !== 'label' && !isEmpty(item[k])) {{
                            html += `<div style="font-size: 0.75rem; color: #6b7280; margin-top: 2px;">`;
                            html += `<span style="font-weight: 600;">${{getFieldDisplay(k)}}:</span> ${{item[k]}}`;
                            html += `</div>`;
                        }}
                    }});
                    
                    html += `</div>`;
                    return html;
                }} else {{
                    return `<div class="json-array-item">${{renderGenericObject(item)}}</div>`;
                }}
            }}
            
            return `<div class="json-array-item">${{JSON.stringify(item)}}</div>`;
        }}

        // 渲染数组
        function renderArray(arr, key) {{
            // 检查是否为recommendations字段（支持不同版本的字段名格式）
            const normalizedKey = key.toLowerCase().replace(/^\\d+\\.\\s*/, '').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
            if (normalizedKey === 'prioritized_recommendations' || normalizedKey === 'recommendations') {{
                return renderRecommendations(arr);
            }}
            
            return arr.map(item => renderArrayItem(item, key)).join('');
        }}

        // 渲染通用对象
        function renderGenericObject(obj) {{
            let html = '<div class="json-object" style="margin-top: 4px;">';
            
            Object.keys(obj).forEach((k, index) => {{
                if (!isEmpty(obj[k])) {{
                    if (index > 0) html += '<div style="height: 8px;"></div>';
                    html += `<div class="json-key" style="font-size: 0.75rem; margin-bottom: 2px;">${{getFieldDisplay(k)}}</div>`;
                    html += renderValue(obj[k], k);
                }}
            }});
            
            html += '</div>';
            return html;
        }}

        // 渲染字符串
        function renderString(value) {{
            return `<div class="json-string" style="font-size: 0.875rem;">${{value}}</div>`;
        }}

        // 渲染数字
        function renderNumber(value) {{
            return `<span class="json-number">${{value}}</span>`;
        }}

        // 渲染布尔值
        function renderBoolean(value) {{
            return `<span class="json-number">${{value ? '✓ Yes' : '✗ No'}}</span>`;
        }}

        // 渲染任意值
        function renderValue(value, key) {{
            if (isEmpty(value)) return '';
            
            if (typeof value === 'string') return renderString(value);
            if (typeof value === 'number') return renderNumber(value);
            if (typeof value === 'boolean') return renderBoolean(value);
            
            if (Array.isArray(value)) return renderArray(value, key);
            
            if (typeof value === 'object') {{
                // 检查是否为AI Assessment Metadata（支持不同版本的字段名格式）
                const normalizedKey = key.toLowerCase().replace(/^\\d+\\.\\s*/, '').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
                if (normalizedKey === 'ai_assessment_metadata') return renderAIMetadata(value);
                if (normalizedKey === 'prioritized_recommendations' || normalizedKey === 'recommendations') {{
                    // 如果是单个对象，包装成数组
                    return renderRecommendations(Array.isArray(value) ? value : [value]);
                }}
                return renderGenericObject(value);
            }}
            
            return JSON.stringify(value);
        }}

        // 渲染整个报告
        function renderReport(report, version, prompt, newFields, versionIdx) {{
            let html = '';
            
            // 获取所有非空字段
            const fields = Object.keys(report).filter(key => !isEmpty(report[key]));
            
            // 渲染每个字段
            fields.forEach(key => {{
                const isNew = newFields[version] && newFields[version].includes(key);
                
                // 判断是否为clinical considerations字段（需要规范化字段名来判断）
                const normalizedKey = key.toLowerCase().replace(/^\\d+\\.\\s*/, '').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
                const isClinicalConsiderations = normalizedKey === 'clinical_considerations';
                
                let cardClass = 'section-card';
                if (isNew) {{
                    cardClass += ' new-feature';
                }}
                
                const cardStyle = isClinicalConsiderations ? 
                    'background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%); border-left: 4px solid #ef4444;' : '';
                
                html += `
                    <div class="${{cardClass}}" style="${{cardStyle}}">
                        <div class="section-title">
                            ${{getFieldDisplay(key)}}
                            ${{isNew ? '<span class="new-badge">NEW</span>' : ''}}
                        </div>
                        ${{renderValue(report[key], key)}}
                    </div>
                `;
            }});
            
            // 添加 Prompt
            if (prompt) {{
                const escapedPrompt = prompt.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                html += `
                    <div class="prompt-section">
                        <div style="font-size: 0.875rem; font-weight: 700; color: #1f2937; margin-bottom: 8px;">
                            📄 System Prompt
                        </div>
                        <div class="prompt-content">${{escapedPrompt}}</div>
                    </div>
                `;
            }}
            
            return html;
        }}

        // 渲染对比
        function renderComparison(patientIndex) {{
            const grid = document.getElementById('comparison-grid');
            grid.innerHTML = '';
            
            const patient = patientsData[patientIndex];
            const newFields = getNewFields(patientIndex);
            
            versions.forEach((version, idx) => {{
                const versionData = patient.versions[version];
                if (!versionData) return;
                
                const report = versionData.baseline_report || versionData;
                const prompt = versionData.prompt || null;
                
                const column = document.createElement('div');
                column.className = 'report-column';
                
                // 获取版本描述和生成时间
                const description = versionDescriptions[version] || '';
                const generatedAt = versionData.generated_at || '';
                
                // 格式化时间显示
                let timeDisplay = '';
                if (generatedAt) {{
                    try {{
                        const date = new Date(generatedAt);
                        timeDisplay = `<p class="text-xs opacity-75 mt-1">Generated: ${{date.toLocaleString()}}</p>`;
                    }} catch (e) {{
                        timeDisplay = `<p class="text-xs opacity-75 mt-1">Generated: ${{generatedAt}}</p>`;
                    }}
                }}
                
                column.innerHTML = `
                    <div class="version-header">
                        <h2 class="text-xl font-bold mb-1">Version ${{idx + 1}}</h2>
                        ${{description ? `<p class="text-sm opacity-90">${{description}}</p>` : ''}}
                        ${{timeDisplay}}
                    </div>
                    ${{renderReport(report, version, prompt, newFields, idx)}}
                `;
                grid.appendChild(column);
            }});
        }}

        // 初始化患者选择器
        function initPatientSelector() {{
            const selector = document.getElementById('patient-selector');
            
            patientsData.forEach((patient, index) => {{
                const option = document.createElement('option');
                option.value = index;
                option.text = `Patient ${{index + 1}} - ${{patient.patient_id.substring(0, 20)}}...`;
                selector.appendChild(option);
            }});
            
            selector.addEventListener('change', (e) => {{
                renderComparison(parseInt(e.target.value));
            }});
        }}

        // 初始化
        initPatientSelector();
        renderComparison(0);
        
        // HIPAA合规信息
        console.log('🔒 HIPAA Notice: Report contains patient health information');
        console.log('📊 Access restricted to authorized healthcare personnel');
        console.log('🛡️ Handle all data according to HIPAA privacy standards');
    </script>
    
    <!-- HIPAA Compliance Footer -->
    <footer class="mt-8 text-center text-sm text-gray-500 border-t pt-4" style="border-color: #d4b896;">
        <p>🔒 <strong>HIPAA Notice</strong> - Contains patient health information | Restricted access only</p>
        <p class="mt-1">Handle all data according to HIPAA privacy and security standards</p>
        
        <!-- Author attribution in bottom left -->
        <div class="mt-4 text-left">
            <span class="text-xs text-gray-400">by Chengyao</span>
        </div>
    </footer>
</body>
</html>
"""
    
    # 写入文件
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"\n✓ HTML file generated: {output_file}")
    print(f"  Patients: {len(patients)}")
    print(f"  Versions: {', '.join(versions)}")


def main():
    parser = argparse.ArgumentParser(
        description='生成带完整样式的动态营养基线报告对比HTML页面'
    )
    parser.add_argument(
        'input_file',
        nargs='?',
        default='nutrition_baseline_prod_8.csv',
        help='输入CSV文件路径（默认：nutrition_baseline_prod_8.csv）'
    )
    parser.add_argument(
        '-o', '--output',
        default='nutrition_comparison.html',
        help='输出HTML文件路径（默认：nutrition_comparison.html）'
    )
    
    args = parser.parse_args()
    
    print(f"Reading CSV file: {args.input_file}")
    print("-" * 80)
    
    # 读取CSV数据
    data = read_csv_versions(args.input_file)
    
    if not data.get('patients'):
        print("Error: No valid data found in CSV")
        sys.exit(1)
    
    # 生成HTML
    generate_html(data, args.output, args.input_file)


if __name__ == '__main__':
    main()

