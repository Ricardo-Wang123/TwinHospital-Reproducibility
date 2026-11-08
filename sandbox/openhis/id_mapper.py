"""
OpenHIS ID 映射器
处理本地系统和OpenHIS系统之间的ID映射
"""

import json
import os
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class OpenHISIDMapper:
    """
    本地ID和OpenHIS ID之间的双向映射管理器

    负责：
    - 维护本地UUID与OpenHIS UUID之间的映射
    - 支持多种实体类型（patient, doctor, encounter, registration等）
    - 持久化映射到磁盘
    - 从磁盘加载映射
    """

    def __init__(self, persist_dir: Optional[str] = None):
        """
        初始化ID映射器

        Args:
            persist_dir: 持久化映射的目录路径
                        如果None，则仅在内存中维护
        """
        self.persist_dir = persist_dir
        if self.persist_dir:
            os.makedirs(self.persist_dir, exist_ok=True)

        # 映射结构：
        # {
        #   'patient': {local_uuid: openhis_uuid},
        #   'doctor': {local_uuid: openhis_uuid},
        #   'encounter': {local_uuid: openhis_uuid},
        #   ...
        # }
        # 以及反向映射用于快速查询
        self.mappings: Dict[str, Dict[str, str]] = {}
        self.reverse_mappings: Dict[str, Dict[str, str]] = {}

        # 加载已有的映射
        if self.persist_dir:
            self._load_from_disk()

    def _get_persist_path(self) -> str:
        """获取持久化文件路径"""
        return os.path.join(self.persist_dir, 'id_mappings.json')

    def set_mapping(
        self,
        entity_type: str,
        local_id: str,
        openhis_id: str
    ) -> None:
        """
        设置ID映射

        Args:
            entity_type: 实体类型 (patient, doctor, encounter, registration等)
            local_id: 本地ID
            openhis_id: OpenHIS ID
        """
        # 初始化entity_type的映射字典（如果不存在）
        if entity_type not in self.mappings:
            self.mappings[entity_type] = {}
        if entity_type not in self.reverse_mappings:
            self.reverse_mappings[entity_type] = {}

        # 检查是否已存在不同的映射
        if local_id in self.mappings[entity_type]:
            old_openhis_id = self.mappings[entity_type][local_id]
            if old_openhis_id != openhis_id:
                logger.warning(
                    f"⚠ 覆盖映射: {entity_type}.{local_id} "
                    f"{old_openhis_id} → {openhis_id}"
                )
                # 清理旧的反向映射
                del self.reverse_mappings[entity_type][old_openhis_id]

        # 设置映射和反向映射
        self.mappings[entity_type][local_id] = openhis_id
        self.reverse_mappings[entity_type][openhis_id] = local_id

        logger.debug(
            f"✓ 映射已设置: {entity_type}.{local_id} ↔ {openhis_id}"
        )

    def get_openhis_id(
        self,
        entity_type: str,
        local_id: str
    ) -> Optional[str]:
        """
        根据本地ID获取OpenHIS ID

        Args:
            entity_type: 实体类型
            local_id: 本地ID

        Returns:
            OpenHIS ID或None
        """
        if entity_type not in self.mappings:
            return None
        return self.mappings[entity_type].get(local_id)

    def get_local_id(
        self,
        entity_type: str,
        openhis_id: str
    ) -> Optional[str]:
        """
        根据OpenHIS ID获取本地ID（反向查询）

        Args:
            entity_type: 实体类型
            openhis_id: OpenHIS ID

        Returns:
            本地ID或None
        """
        if entity_type not in self.reverse_mappings:
            return None
        return self.reverse_mappings[entity_type].get(openhis_id)

    def get_or_create_openhis_id(
        self,
        entity_type: str,
        local_id: str,
        openhis_id: str = None
    ) -> str:
        """
        获取已有的映射，或创建新映射

        Args:
            entity_type: 实体类型
            local_id: 本地ID
            openhis_id: OpenHIS ID（如果已知）

        Returns:
            OpenHIS ID
        """
        existing = self.get_openhis_id(entity_type, local_id)
        if existing:
            return existing

        if openhis_id is None:
            raise ValueError(
                f"找不到{entity_type}.{local_id}的映射，"
                f"且未提供openhis_id"
            )

        self.set_mapping(entity_type, local_id, openhis_id)
        return openhis_id

    def has_mapping(
        self,
        entity_type: str,
        local_id: str
    ) -> bool:
        """
        检查是否存在映射

        Args:
            entity_type: 实体类型
            local_id: 本地ID

        Returns:
            是否存在
        """
        if entity_type not in self.mappings:
            return False
        return local_id in self.mappings[entity_type]

    def get_all_mappings_for_type(self, entity_type: str) -> Dict[str, str]:
        """
        获取某个类型的所有映射

        Args:
            entity_type: 实体类型

        Returns:
            {local_id: openhis_id} 字典
        """
        return self.mappings.get(entity_type, {}).copy()

    def clear_entity_type(self, entity_type: str) -> None:
        """
        清空某个类型的所有映射

        Args:
            entity_type: 实体类型
        """
        if entity_type in self.mappings:
            del self.mappings[entity_type]
        if entity_type in self.reverse_mappings:
            del self.reverse_mappings[entity_type]
        logger.info(f"✓ 已清空{entity_type}的映射")

    def clear_all(self) -> None:
        """清空所有映射"""
        self.mappings.clear()
        self.reverse_mappings.clear()
        logger.info("✓ 已清空所有映射")

    def persist_to_disk(self) -> bool:
        """
        将映射持久化到磁盘

        Returns:
            是否成功
        """
        if not self.persist_dir:
            logger.warning("⚠ 未配置持久化目录")
            return False

        try:
            persist_path = self._get_persist_path()
            with open(persist_path, 'w', encoding='utf-8') as f:
                json.dump(self.mappings, f, ensure_ascii=False, indent=2)
            logger.info(f"✓ 映射已持久化到: {persist_path}")
            return True

        except Exception as e:
            logger.error(f"✗ 持久化映射失败: {e}")
            return False

    def _load_from_disk(self) -> bool:
        """
        从磁盘加载映射

        Returns:
            是否成功
        """
        persist_path = self._get_persist_path()

        if not os.path.exists(persist_path):
            logger.info(f"ℹ 映射文件不存在: {persist_path}")
            return False

        try:
            with open(persist_path, 'r', encoding='utf-8') as f:
                self.mappings = json.load(f)

            # 重建反向映射
            self.reverse_mappings.clear()
            for entity_type, mapping in self.mappings.items():
                self.reverse_mappings[entity_type] = {
                    v: k for k, v in mapping.items()
                }

            logger.info(
                f"✓ 已从磁盘加载映射: {persist_path} "
                f"({sum(len(m) for m in self.mappings.values())} 条)"
            )
            return True

        except Exception as e:
            logger.error(f"✗ 加载映射失败: {e}")
            return False

    def export_to_dict(self) -> Dict[str, Any]:
        """
        导出所有映射为字典

        Returns:
            {entity_type: {local_id: openhis_id}} 格式的字典
        """
        return {
            entity_type: mapping.copy()
            for entity_type, mapping in self.mappings.items()
        }

    def import_from_dict(self, data: Dict[str, Dict[str, str]]) -> None:
        """
        从字典导入映射

        Args:
            data: {entity_type: {local_id: openhis_id}} 格式的字典
        """
        self.clear_all()
        for entity_type, mapping in data.items():
            for local_id, openhis_id in mapping.items():
                self.set_mapping(entity_type, local_id, openhis_id)
        logger.info(f"✓ 已导入映射")

    def get_stats(self) -> Dict[str, int]:
        """
        获取映射统计信息

        Returns:
            {entity_type: count} 字典
        """
        return {
            entity_type: len(mapping)
            for entity_type, mapping in self.mappings.items()
        }
