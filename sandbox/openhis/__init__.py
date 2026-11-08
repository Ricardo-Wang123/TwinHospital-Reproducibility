"""
OpenHIS 集成模块

提供与OpenHIS系统的集成适配层
"""

from .client import OpenHISAPIClient
from .id_mapper import OpenHISIDMapper
from .adapter import OpenHISAdapter

__all__ = [
    'OpenHISAPIClient',
    'OpenHISIDMapper',
    'OpenHISAdapter'
]

__version__ = '1.0.0'
