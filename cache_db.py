#!/usr/bin/env python3
"""
Cache Database Module
缓存数据库模块

Provides SQLite-based caching for downloaded images and AI meal summaries.
提供基于 SQLite 的图片下载和 AI meal summary 缓存。
"""
import sqlite3
import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


class CacheDB:
    """SQLite-based cache for images and AI summaries."""
    
    def __init__(self, db_path: Path = Path("./cache.db")):
        """
        Initialize cache database.
        初始化缓存数据库。
        
        Args:
            db_path: Path to SQLite database file / SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Check if old table structure exists and migrate
        # 检查是否存在旧表结构并迁移
        try:
            cursor.execute("PRAGMA table_info(image_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'food_log_id' not in columns:
                # Old table structure, need to migrate
                # 旧表结构，需要迁移
                print("[INFO] Migrating image_cache table to new structure...")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS image_cache_new (
                        image_url TEXT,
                        food_log_id TEXT,
                        image_index INTEGER,
                        local_path TEXT NOT NULL,
                        file_hash TEXT,
                        download_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        file_size INTEGER,
                        PRIMARY KEY (food_log_id, image_index),
                        UNIQUE(image_url)
                    )
                """)
                # Copy data from old table (food_log_id and image_index will be NULL)
                # 从旧表复制数据（food_log_id 和 image_index 将为 NULL）
                cursor.execute("""
                    INSERT INTO image_cache_new (image_url, local_path, file_hash, download_time, file_size)
                    SELECT image_url, local_path, file_hash, download_time, file_size
                    FROM image_cache
                """)
                cursor.execute("DROP TABLE image_cache")
                cursor.execute("ALTER TABLE image_cache_new RENAME TO image_cache")
                conn.commit()
                print("[OK] Migration completed")
        except sqlite3.OperationalError:
            # Table doesn't exist, create new one
            # 表不存在，创建新表
            pass
        
        # Image cache table
        # 图片缓存表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS image_cache (
                image_url TEXT,
                food_log_id TEXT,
                image_index INTEGER,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                download_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_size INTEGER,
                PRIMARY KEY (food_log_id, image_index),
                UNIQUE(image_url)
            )
        """)
        
        # AI summary cache table
        # AI summary 缓存表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_summary_cache (
                cache_key TEXT PRIMARY KEY,
                food_log_id TEXT,
                image_url TEXT NOT NULL,
                patient_notes_hash TEXT,
                summary_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Check if food_log_id column exists, if not add it
        # 检查是否存在 food_log_id 列，如果不存在则添加
        try:
            cursor.execute("PRAGMA table_info(ai_summary_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'food_log_id' not in columns:
                print("[INFO] Adding food_log_id column to ai_summary_cache table...")
                cursor.execute("ALTER TABLE ai_summary_cache ADD COLUMN food_log_id TEXT")
                conn.commit()
                print("[OK] Migration completed")
        except sqlite3.OperationalError:
            pass
        
        # Create indexes for faster lookups
        # 创建索引以加快查询
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_image_url ON image_cache(image_url)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_food_log_id ON image_cache(food_log_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_summary_image_url ON ai_summary_cache(image_url)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_summary_notes_hash ON ai_summary_cache(patient_notes_hash)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_summary_food_log_id ON ai_summary_cache(food_log_id)
        """)
        
        # Period insight cache table (weekly/monthly insights)
        # 周期洞察缓存表（周/月洞察）
        # Cache is based on query date (end_date), not date range
        # 缓存基于查询日期（end_date），而不是日期范围
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS period_insight_cache (
                cache_key TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL,
                period_type TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                language TEXT NOT NULL,
                insight_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(patient_id, period_type, end_date, language)
            )
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_period_insight_patient ON period_insight_cache(patient_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_period_insight_type ON period_insight_cache(period_type)
        """)
        
        conn.commit()
        conn.close()
    
    def get_image_cache(
        self,
        food_log_id: Optional[str] = None,
        image_index: Optional[int] = None,
        image_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached image information.
        获取缓存的图片信息。
        
        Args:
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_index: Image index (0-based) / 图片索引（从0开始）
            image_url: Image URL (fallback) / 图片URL（备用）
            
        Returns:
            Dict with local_path, file_hash, download_time, file_size or None
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Prefer food_log_id + image_index lookup
        # 优先使用 food_log_id + image_index 查询
        if food_log_id is not None and image_index is not None:
            cursor.execute("""
                SELECT local_path, file_hash, download_time, file_size
                FROM image_cache
                WHERE food_log_id = ? AND image_index = ?
            """, (food_log_id, image_index))
        elif image_url:
            # Fallback to image_url lookup
            # 回退到 image_url 查询
            cursor.execute("""
                SELECT local_path, file_hash, download_time, file_size
                FROM image_cache
                WHERE image_url = ?
            """, (image_url,))
        else:
            conn.close()
            return None
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            local_path, file_hash, download_time, file_size = row
            # Check if file still exists
            # 检查文件是否仍然存在
            if Path(local_path).exists():
                return {
                    "local_path": local_path,
                    "file_hash": file_hash,
                    "download_time": download_time,
                    "file_size": file_size
                }
            else:
                # File deleted, remove from cache
                # 文件已删除，从缓存中移除
                if food_log_id is not None and image_index is not None:
                    self.remove_image_cache(food_log_id=food_log_id, image_index=image_index)
                elif image_url:
                    self.remove_image_cache(image_url=image_url)
        
        return None
    
    def save_image_cache(
        self,
        local_path: str,
        food_log_id: Optional[str] = None,
        image_index: Optional[int] = None,
        image_url: Optional[str] = None,
        file_hash: Optional[str] = None,
        file_size: Optional[int] = None
    ):
        """
        Save image cache entry.
        保存图片缓存条目。
        
        Args:
            local_path: Local file path / 本地文件路径
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_index: Image index (0-based) / 图片索引（从0开始）
            image_url: Image URL (optional) / 图片URL（可选）
            file_hash: File hash (optional) / 文件哈希值（可选）
            file_size: File size in bytes (optional) / 文件大小（字节，可选）
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO image_cache
            (food_log_id, image_index, image_url, local_path, file_hash, download_time, file_size)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """, (food_log_id, image_index, image_url or "", str(local_path), file_hash, file_size))
        
        conn.commit()
        conn.close()
    
    def remove_image_cache(
        self,
        food_log_id: Optional[str] = None,
        image_index: Optional[int] = None,
        image_url: Optional[str] = None
    ):
        """
        Remove image cache entry.
        移除图片缓存条目。
        
        Args:
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_index: Image index (0-based) / 图片索引（从0开始）
            image_url: Image URL (fallback) / 图片URL（备用）
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        if food_log_id is not None and image_index is not None:
            cursor.execute("DELETE FROM image_cache WHERE food_log_id = ? AND image_index = ?", (food_log_id, image_index))
        elif image_url:
            cursor.execute("DELETE FROM image_cache WHERE image_url = ?", (image_url,))
        
        conn.commit()
        conn.close()
    
    def _compute_cache_key(self, food_log_id: Optional[str] = None, image_url: Optional[str] = None, patient_notes: Optional[str] = None) -> str:
        """
        Compute cache key for AI summary.
        计算 AI summary 的缓存键。
        
        Args:
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_url: Image URL (fallback if no food_log_id) / 图片URL（如果没有food_log_id时使用）
            patient_notes: Patient notes (optional) / 病人备注（可选）
            
        Returns:
            Cache key string / 缓存键字符串
        """
        # Prefer food_log_id for cache key (more reliable)
        # 优先使用 food_log_id 作为缓存键（更可靠）
        if food_log_id:
            key_data = f"food_log_id:{food_log_id}"
            if patient_notes:
                key_data += "|notes:" + patient_notes
        else:
            # Fallback to image_url if no food_log_id
            # 如果没有 food_log_id，回退到 image_url
            key_data = image_url or ""
            if patient_notes:
                key_data += "|" + patient_notes
        
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def _compute_notes_hash(self, patient_notes: Optional[str] = None) -> Optional[str]:
        """
        Compute hash of patient notes.
        计算病人备注的哈希值。
        
        Args:
            patient_notes: Patient notes / 病人备注
            
        Returns:
            Hash string or None / 哈希字符串或None
        """
        if not patient_notes:
            return None
        return hashlib.md5(patient_notes.encode('utf-8')).hexdigest()
    
    def get_ai_summary_cache(
        self,
        food_log_id: Optional[str] = None,
        image_url: Optional[str] = None,
        patient_notes: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached AI summary.
        获取缓存的 AI summary。
        
        Args:
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_url: Image URL (fallback if no food_log_id) / 图片URL（如果没有food_log_id时使用）
            patient_notes: Patient notes (optional) / 病人备注（可选）
            
        Returns:
            Parsed summary dict or None / 解析后的 summary 字典或None
        """
        # Normalize patient_notes: treat empty string as None
        # 规范化 patient_notes：将空字符串视为 None
        if patient_notes is not None and not patient_notes.strip():
            patient_notes = None
        
        cache_key = self._compute_cache_key(food_log_id, image_url, patient_notes)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT summary_json, created_at
            FROM ai_summary_cache
            WHERE cache_key = ?
        """, (cache_key,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            summary_json, created_at = row
            try:
                return json.loads(summary_json)
            except json.JSONDecodeError:
                # Invalid JSON, remove from cache
                # 无效的 JSON，从缓存中移除
                self.remove_ai_summary_cache(food_log_id, image_url, patient_notes)
        
        return None
    
    def save_ai_summary_cache(
        self,
        summary: Dict[str, Any],
        food_log_id: Optional[str] = None,
        image_url: Optional[str] = None,
        patient_notes: Optional[str] = None
    ):
        """
        Save AI summary cache entry.
        保存 AI summary 缓存条目。
        
        Args:
            summary: Summary dict to cache / 要缓存的 summary 字典
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_url: Image URL (fallback if no food_log_id) / 图片URL（如果没有food_log_id时使用）
            patient_notes: Patient notes (optional) / 病人备注（可选）
        """
        # Normalize patient_notes: treat empty string as None
        # 规范化 patient_notes：将空字符串视为 None
        if patient_notes is not None and not patient_notes.strip():
            patient_notes = None
        
        cache_key = self._compute_cache_key(food_log_id, image_url, patient_notes)
        notes_hash = self._compute_notes_hash(patient_notes)
        summary_json = json.dumps(summary, ensure_ascii=False)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO ai_summary_cache
            (cache_key, food_log_id, image_url, patient_notes_hash, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (cache_key, food_log_id or "", image_url or "", notes_hash, summary_json))
        
        conn.commit()
        conn.close()
    
    def remove_ai_summary_cache(
        self,
        food_log_id: Optional[str] = None,
        image_url: Optional[str] = None,
        patient_notes: Optional[str] = None
    ):
        """
        Remove AI summary cache entry.
        移除 AI summary 缓存条目。
        
        Args:
            food_log_id: Food log ID (preferred) / Food log ID（优先使用）
            image_url: Image URL (fallback if no food_log_id) / 图片URL（如果没有food_log_id时使用）
            patient_notes: Patient notes (optional) / 病人备注（可选）
        """
        cache_key = self._compute_cache_key(food_log_id, image_url, patient_notes)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM ai_summary_cache WHERE cache_key = ?", (cache_key,))
        
        conn.commit()
        conn.close()
    
    def get_ai_summaries_by_food_log_ids(self, food_log_ids: list) -> Dict[str, Dict[str, Any]]:
        """
        Get all cached AI summaries for a list of food log IDs.
        获取指定food log IDs列表的所有缓存AI summaries。
        
        Args:
            food_log_ids: List of food log IDs / Food log ID列表
            
        Returns:
            Dict mapping food_log_id to list of summary dicts / 映射food_log_id到summary列表的字典
        """
        if not food_log_ids:
            return {}
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        placeholders = ','.join(['?'] * len(food_log_ids))
        
        cursor.execute(f"""
            SELECT food_log_id, summary_json
            FROM ai_summary_cache
            WHERE food_log_id IN ({placeholders}) AND food_log_id IS NOT NULL
        """, food_log_ids)
        
        results = {}
        for row in cursor.fetchall():
            food_log_id, summary_json = row
            try:
                summary = json.loads(summary_json)
                if food_log_id not in results:
                    results[food_log_id] = []
                results[food_log_id].append(summary)
            except json.JSONDecodeError:
                continue
        
        conn.close()
        return results
    
    def check_patient_cache_status(self, food_log_ids: list) -> Dict[str, Any]:
        """
        Check cache status for a list of food log IDs.
        检查一系列 food log ID 的缓存状态。
        
        Args:
            food_log_ids: List of food log IDs / Food log ID 列表
            
        Returns:
            Dict with cached_images, cached_summaries, total_food_logs / 
            包含 cached_images, cached_summaries, total_food_logs 的字典
        """
        if not food_log_ids:
            return {
                "cached_images": 0,
                "cached_summaries": 0,
                "total_food_logs": 0
            }
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Count cached images for these food log IDs
        # 统计这些 food log ID 的缓存图片数
        placeholders = ','.join(['?'] * len(food_log_ids))
        cursor.execute(f"""
            SELECT COUNT(DISTINCT food_log_id || '_' || CAST(image_index AS TEXT))
            FROM image_cache
            WHERE food_log_id IN ({placeholders})
        """, food_log_ids)
        
        cached_images = cursor.fetchone()[0] or 0
        
        # For AI summaries, we need to check by image_url
        # Get all image_urls for these food log IDs from image_cache
        # 对于 AI 摘要，需要通过 image_url 检查
        # 从 image_cache 获取这些 food log ID 的所有 image_url
        cursor.execute(f"""
            SELECT DISTINCT image_url
            FROM image_cache
            WHERE food_log_id IN ({placeholders}) AND image_url IS NOT NULL
        """, food_log_ids)
        
        image_urls = [row[0] for row in cursor.fetchall()]
        
        # Count cached AI summaries for these food log IDs
        # 优先通过 food_log_id 检查
        # 统计这些 food log ID 的缓存 AI 摘要数
        cursor.execute(f"""
            SELECT COUNT(DISTINCT food_log_id)
            FROM ai_summary_cache
            WHERE food_log_id IN ({placeholders}) AND food_log_id IS NOT NULL AND food_log_id != ''
        """, food_log_ids)
        
        cached_summaries_by_id = cursor.fetchone()[0] or 0
        
        # Also check by image_url as fallback (for old cache entries without food_log_id)
        # 也通过 image_url 检查作为回退（对于没有 food_log_id 的旧缓存条目）
        cached_summaries_by_url = 0
        if image_urls:
            url_placeholders = ','.join(['?'] * len(image_urls))
            # Get image URLs that don't have food_log_id in cache
            # 获取缓存中没有 food_log_id 的 image URL
            id_placeholders = ','.join(['?'] * len(food_log_ids))
            cursor.execute(f"""
                SELECT COUNT(DISTINCT image_url)
                FROM ai_summary_cache
                WHERE image_url IN ({url_placeholders}) 
                AND (food_log_id IS NULL OR food_log_id = '' OR food_log_id NOT IN ({id_placeholders}))
            """, image_urls + food_log_ids)
            
            cached_summaries_by_url = cursor.fetchone()[0] or 0
        
        cached_summaries = cached_summaries_by_id + cached_summaries_by_url
        
        conn.close()
        
        return {
            "cached_images": cached_images,
            "cached_summaries": cached_summaries,
            "total_food_logs": len(food_log_ids)
        }
    
    def clear_all_cache(self):
        """Clear all cache entries."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM image_cache")
        cursor.execute("DELETE FROM ai_summary_cache")
        cursor.execute("DELETE FROM period_insight_cache")
        
        conn.commit()
        conn.close()
    
    def clear_period_insight_cache(self):
        """
        Clear all period insight cache entries (weekly and monthly).
        清空所有周期洞察缓存条目（周和月）。
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM period_insight_cache")
        conn.commit()
        conn.close()
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        获取缓存统计信息。
        
        Returns:
            Dict with cache statistics / 包含缓存统计信息的字典
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM image_cache")
        image_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM ai_summary_cache")
        summary_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(file_size) FROM image_cache WHERE file_size IS NOT NULL")
        total_size = cursor.fetchone()[0] or 0
        
        conn.close()
        
        cursor.execute("SELECT COUNT(*) FROM period_insight_cache")
        period_insight_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "image_cache_count": image_count,
            "ai_summary_cache_count": summary_count,
            "period_insight_cache_count": period_insight_count,
            "total_image_size_bytes": total_size,
            "total_image_size_mb": round(total_size / (1024 * 1024), 2) if total_size > 0 else 0
        }
    
    def get_period_insight_cache(
        self,
        patient_id: str,
        period_type: str,  # 'weekly', 'monthly', or 'food_swapping'
        end_date: str,  # Query date (YYYY-MM-DD) - cache is based on this date
        language: str = 'zh',
        start_date: Optional[str] = None  # Kept for backward compatibility, not used in cache key
    ) -> Optional[str]:
        """
        Get cached period insight.
        获取缓存的周期洞察。
        
        Cache is based on the query date (end_date), not the date range.
        This ensures that each day has its own cache entry.
        缓存基于查询日期（end_date），而不是日期范围。这确保每一天都有自己的缓存条目。
        
        Args:
            patient_id: Patient ID / 病人ID
            period_type: 'weekly', 'monthly', or 'food_swapping' / 'weekly'、'monthly' 或 'food_swapping'
            end_date: Query date string (YYYY-MM-DD) / 查询日期字符串（YYYY-MM-DD）
            language: Language code / 语言代码
            start_date: Start date (deprecated, kept for compatibility) / 开始日期（已弃用，保留以兼容）
            
        Returns:
            Cached insight text or None / 缓存的洞察文本或None
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Cache key is based on patient_id, period_type, end_date (query date), and language
        # 缓存键基于 patient_id, period_type, end_date（查询日期）和 language
        cursor.execute("""
            SELECT insight_text, created_at
            FROM period_insight_cache
            WHERE patient_id = ? AND period_type = ? AND end_date = ? AND language = ?
        """, (patient_id, period_type, end_date, language))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            insight_text, created_at = row
            return insight_text
        
        return None
    
    def save_period_insight_cache(
        self,
        patient_id: str,
        period_type: str,  # 'weekly', 'monthly', or 'food_swapping'
        end_date: str,  # Query date (YYYY-MM-DD) - cache is based on this date
        insight_text: str,
        language: str = 'zh',
        start_date: Optional[str] = None  # Kept for backward compatibility, stored but not used in cache key
    ):
        """
        Save period insight to cache.
        保存周期洞察到缓存。
        
        Cache is based on the query date (end_date), not the date range.
        This ensures that each day has its own cache entry.
        缓存基于查询日期（end_date），而不是日期范围。这确保每一天都有自己的缓存条目。
        
        Args:
            patient_id: Patient ID / 病人ID
            period_type: 'weekly', 'monthly', or 'food_swapping' / 'weekly'、'monthly' 或 'food_swapping'
            end_date: Query date string (YYYY-MM-DD) / 查询日期字符串（YYYY-MM-DD）
            insight_text: Generated insight text / 生成的洞察文本
            language: Language code / 语言代码
            start_date: Start date (deprecated, stored for reference) / 开始日期（已弃用，存储以供参考）
        """
        # Generate cache key based on patient_id, period_type, end_date (query date), and language
        # 基于 patient_id, period_type, end_date（查询日期）和 language 生成缓存键
        cache_key_data = f"{patient_id}|{period_type}|{end_date}|{language}"
        cache_key = hashlib.md5(cache_key_data.encode('utf-8')).hexdigest()
        
        # Use end_date as start_date if not provided (for backward compatibility)
        # 如果未提供 start_date，则使用 end_date（向后兼容）
        if start_date is None:
            start_date = end_date
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO period_insight_cache
            (cache_key, patient_id, period_type, start_date, end_date, language, insight_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (cache_key, patient_id, period_type, start_date, end_date, language, insight_text))
        
        conn.commit()
        conn.close()

