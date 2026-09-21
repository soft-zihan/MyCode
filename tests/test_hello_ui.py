import pytest

from scripts.hello_ui import hello


class TestHello:
    """问候函数测试"""

    def test_hello_normal(self):
        """正常输入: 英文名"""
        result = hello("World")
        assert result == "你好, World!"
        assert isinstance(result, str)

    def test_hello_chinese(self):
        """正常输入: 中文名"""
        result = hello("小明")
        assert result == "你好, 小明!"
        assert isinstance(result, str)

    def test_hello_empty(self):
        """边界: 空字符串"""
        result = hello("")
        assert result == "你好, !"
        assert isinstance(result, str)