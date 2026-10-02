# asd-ste100-zh-skill

面向中文技术内容的受控语言改写技能，借鉴 [asd-ste100-skill](https://github.com/danyuchn/asd-ste100-skill) 的目标与工作方式。它帮助消除工具说明、错误信息、代理指令、操作步骤和状态报告中的歧义，同时保留原文的事实、条件、例外和不确定性。

**这不是 ASD-STE100 的官方中文版本，也不提供正式标准合规认证。** 中文规则是独立适配，不能机械套用英文词典或语法规则。

## 安装

将本仓库克隆到 Codex 的技能目录，目录名使用技能名：

```sh
git clone https://github.com/Layau-code/asd-ste100-zh-skill.git ~/.codex/skills/asd-ste100-zh
```

然后在 Codex 中使用 `$asd-ste100-zh`，或直接提出“用受控中文改写这段技术说明”等请求。

## 内容

- [`SKILL.md`](SKILL.md)：适用范围、规则和改写流程。
- [`references/chinese-rules.md`](references/chinese-rules.md)：详细中文规则。
- [`examples/before-after.md`](examples/before-after.md)：改写前后示例。
- [`scripts/zh-lint.py`](scripts/zh-lint.py)：只做机械首轮扫描，不替代语义判断。

运行检查器自测：

```sh
python3 scripts/zh-lint.py --selftest
```

## 致谢与许可

本项目借鉴 Dustin Yuchen Teng 的 [asd-ste100-skill](https://github.com/danyuchn/asd-ste100-skill)。许可证见 [`LICENSE`](LICENSE)。
