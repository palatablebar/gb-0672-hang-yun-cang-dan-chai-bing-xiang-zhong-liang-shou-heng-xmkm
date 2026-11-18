# 深度登记 航运舱单拆并箱与重量守恒

规模：中型。使用者：货代单证员。核心规则：主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 舱单采信 | 执行并复核舱单采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 包装映射 | 执行并复核包装映射 | `POST /api/packaging` | `app/modules/packaging/rules.py` | `tests/test_packaging.py` |
| M3 拆并编组 | 执行并复核拆并编组 | `POST /api/grouping` | `app/modules/grouping/rules.py` | `tests/test_grouping.py` |
| M4 危险品检查 | 执行并复核危险品检查 | `POST /api/hazard` | `app/modules/hazard/rules.py` | `tests/test_hazard.py` |
| M5 净毛重核验 | 执行并复核净毛重核验 | `POST /api/weight` | `app/modules/weight/rules.py` | `tests/test_weight.py` |
| M6 报关对账 | 执行并复核报关对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 版本封存 | 执行并复核版本封存 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码回退 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_source` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_source` |
| TRAP-04 | 分段表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_segment_header` |
| TRAP-05 | 缺列隔离 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_isolation` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_conflict_all_isolated` |
| TRAP-18 | 空态三分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_three_empty_states` |
| TRAP-15 | 吨与包装换算 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_pallet_real` |
| TRAP-07 | 空值哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_sentinel_real` |
| TRAP-06 | 全角千分位 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_fullwidth_decimal` |
| TRAP-20 | 拆并依赖环 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_cycle_real` |
| TRAP-21 | 缺失父级 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_missing_parent_real` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。

## 逐张主单复核与交接封存（M7 扩展）

| 能力 | 入口 | 核心文件 | 测试 |
|---|---|---|---|
| 逐主单证据指纹与阻断归因 | `GET /api/runs/{run_id}/worklist` | `app/review.py` | `tests/test_review_domain.py` |
| 单张/整批确认（不重签未变主单） | `POST /api/runs/{run_id}/roots/{root_id}/confirm`、`/confirm-batch` | `app/store.py` | `tests/test_review_workflow.py` |
| 补证裁决：选择性重签/整批重置 | `POST /api/runs/{run_id}/supplement` | `app/review.py::adjudicate` | `tests/test_review_workflow.py` |
| 交接封存与版本原子切换 | `POST /api/runs/{run_id}/seal`、`GET /api/handoff/{run_id}` | `app/archive.py::build_handoff`、`app/store.py::seal_handoff` | `tests/test_handoff_atomicity.py` |
| 幂等重试与并发冲突 | 全部写接口 `request_token`、`revision` | `app/store.py` | `tests/test_review_workflow.py`、`tests/test_handoff_atomicity.py` |

裁决口径：证据按主单子树切分（重量核验、危险品、采信隔离各归其主；箱级不相容归因到使用该箱的主单；缺父级、依赖环、报关总量差异为封存级阻断）。补证后主单集合不变且指纹可逐张对账时，仅变化主单转“需重签”；主单集合变化或历史指纹缺失时整批转“待复核”，避免封存件引用说不清来源的依据。
