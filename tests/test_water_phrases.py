"""常见说法应归一到同一水体任务，额外目标不得被默认工具替换。"""
import unittest
from core.session import Session
from tools.task_contract import ensure_task_contract
from tools.water_workflow import is_basic_water_request


class WaterPhraseTests(unittest.TestCase):
    def session(self, text):
        session = Session()
        session.user_request = text
        session.data_context = {"files": [{"file_name": "water_03/image.tif", "readable": True}]}
        ensure_task_contract(session)
        return session

    def test_common_phrases_keep_raw_text_and_canonical_water_target(self):
        phrases = ["帮我提取water_03里的水体，告诉我有多少。",
                   "帮我圈出water_03中的水面，看看占多大面积。",
                   "water_03里面的水域主要在哪？有多大？",
                   "看看water_03里的湖泊和河流，标出来并给出面积。"]
        for text in phrases:
            with self.subTest(text=text):
                session = self.session(text)
                self.assertEqual(session.task_contract["original_request"], text)
                self.assertEqual(session.task_contract["targets"], ["水体"])
                self.assertEqual(session.task_contract["inputs"], ["water_03/image.tif"])
                self.assertTrue(is_basic_water_request(session))

    def test_depth_quality_vectors_and_explicit_methods_are_not_area_extraction(self):
        for text in ["提取water_03的水体并计算水深", "分析water_03的湖泊水质", "测量water_03的河流宽度",
                     "提取water_03的水域并输出GeoJSON矢量", "提取water_03的水面，只用NDWI",
                     "提取water_03的水体变化", "提取water_03的水体和道路"]:
            with self.subTest(text=text):
                self.assertFalse(is_basic_water_request(self.session(text)))

    def test_water_word_in_a_filename_does_not_supply_the_goal(self):
        session = self.session("请检查data/水域/image.tif的文件格式")
        self.assertNotIn("水体", session.task_contract["targets"])
        self.assertFalse(is_basic_water_request(session))

    def test_method_word_in_a_filename_is_not_an_explicit_algorithm_choice(self):
        session = self.session("提取data/NDWI_scene/image.tif里的水面并统计面积")
        self.assertTrue(is_basic_water_request(session))

    def test_majority_of_water_missed_is_a_quality_issue_not_an_execution_pass(self):
        from benchmarks.metrics import quality_warnings
        result = quality_warnings({"tp": 4, "fn": 6, "fp": 1, "missing_predictions": 0})
        self.assertIn("majority_water_unconfirmed", result["flags"])
        self.assertIn("完整水体", "".join(result["messages"]))
        result = quality_warnings({"tp": 8, "fn": 2, "fp": 1, "missing_predictions": 0})
        self.assertEqual(result["flags"], [])
        result = quality_warnings({"tp": 1, "fn": 0, "fp": 4, "missing_predictions": 2})
        self.assertIn("majority_candidates_wrong", result["flags"])
        self.assertIn("incomplete_prediction", result["flags"])


if __name__ == "__main__":
    unittest.main()
