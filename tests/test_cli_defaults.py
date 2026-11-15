"""cli 配置默认值测试：配置文件没写的键回退 init-config 模板默认值。"""

import yaml

from curator_views import cli


def write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfigDefaults:
    def test_missing_keys_use_template_defaults(self, tmp_path):
        cfg = cli._load_config(write_config(tmp_path, 'clan_id: "123"\n'))

        assert cfg["clan_id"] == "123"
        assert cfg["volatility"] == 0.10
        assert cfg["growth_factor"] == 1.5
        assert cfg["growth_rate"] == 0.0
        assert cfg["boost_mode"] == "stable"
        assert cfg["db_path"] == "views.db"
        assert cfg["cookie_file"] == "cookie.txt"
        assert cfg["threads"] == 1
        assert cfg["wait_max"] == 16.0
        assert cfg["daily_times"] == []
        assert cfg["daily_time"] == "10:00"
        assert cfg["boost_distribution"] == "ratio"
        assert cfg["executor_cmd"] == ""

    def test_user_values_override_defaults(self, tmp_path):
        cfg = cli._load_config(
            write_config(
                tmp_path,
                'clan_id: "1"\nvolatility: 0.25\nthreads: 3\n',
            )
        )

        assert cfg["volatility"] == 0.25
        assert cfg["threads"] == 3
        # 未覆盖的键仍用模板默认值
        assert cfg["growth_factor"] == 1.5

    def test_null_values_fall_back_to_defaults(self, tmp_path):
        # yaml 里只写键名（值为 null）等价于“没写”，回退默认值
        cfg = cli._load_config(
            write_config(tmp_path, 'clan_id: "1"\nvolatility:\nthreads:\n')
        )

        assert cfg["volatility"] == 0.10
        assert cfg["threads"] == 1

    def test_explicit_empty_values_are_kept(self, tmp_path):
        # 显式空串是有效值（例如主动关闭 Cookie），不能被默认值覆盖
        cfg = cli._load_config(
            write_config(
                tmp_path,
                'clan_id: "1"\ncookie_file: ""\nntfy_min_level: ""\n',
            )
        )

        assert cfg["cookie_file"] == ""
        assert cfg["ntfy_min_level"] == ""

    def test_empty_file_uses_all_defaults(self, tmp_path):
        cfg = cli._load_config(write_config(tmp_path, ""))

        assert cfg["clan_id"] == ""
        assert cfg["db_path"] == "views.db"
        assert cfg["volatility"] == 0.10


class TestDefaultSources:
    def test_template_values_match_default_config_values(self):
        """模板与默认值字典必须同源，避免两处数字漂移。"""
        template = yaml.safe_load(cli.DEFAULT_CONFIG)
        extra_keys = {"boost_distribution", "executor_cmd"}

        assert set(template) == set(cli.DEFAULT_CONFIG_VALUES) - extra_keys
        for key, value in template.items():
            assert cli.DEFAULT_CONFIG_VALUES[key] == value

    def test_scheduler_config_uses_template_defaults(self, tmp_path):
        sched = cli._scheduler_config(
            {}, boost_enabled=False, dry_run=True, cfg_dir=tmp_path
        )

        assert sched["growth_factor"] == 1.5
        assert sched["volatility"] == 0.10
        assert sched["boost_distribution"] == "ratio"
        assert sched["threads"] == 1