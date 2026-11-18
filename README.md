# 航运舱单拆并箱与重量守恒

给货代单证员使用。主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

七个业务模块：舱单采信、包装映射、拆并编组、危险品检查、净毛重核验、报关对账、版本封存。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18241 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。干净批42个节点、6个主单，件数240、净重2400kg、毛重2640kg；现场脏批包含父级缺失、依赖环、危险品不相容及1kg毛重差异，封存状态为待处理。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

## 逐张主单复核与交接封存

封存页（`/archive`）是单证员工作台，提供可交接复核清单：每张主单列出子树节点数、核验依据指纹（证据内容 SHA256，证据不变指纹不变）、逐条阻断原因与当前复核状态。阻断项未解除的主单按钮置灰，只有证据已确认的当前主单能进入封存件。

- `GET /api/runs/{run_id}/worklist` 复核清单；`POST /api/runs/{run_id}/roots/{root_id}/confirm` 确认单张主单；`POST /api/runs/{run_id}/confirm-batch` 整批确认（已确认且证据未变的主单幂等跳过，不重复重签）。
- 别单补证走 `POST /api/runs/{run_id}/supplement`（传补证后的完整 `csv_text`，沿用该批不可变上下文）。系统按封存件能否逐张说明依据裁决：主单集合不变、历史指纹可对账时为 `selective`，仅证据指纹变化的主单置为“需重签”，别单保留原结论；主单集合变化或历史指纹缺失时为 `reset`，整批回到待复核。裁决理由与变化主单写回清单。
- `POST /api/runs/{run_id}/seal` 生成交接封存件：只装证据已确认且指纹为当前值的主单，包内 `handoff/manifest.json` 逐张写清依据指纹、证据版本与源快照 `sha256`。切换在单事务内先挂新版再摘旧版，失败回滚，旧版交接包保留可下载；当前版本唯一下载入口 `GET /api/handoff/{run_id}`，历史版本 `GET /api/runs/{run_id}/packages/{id}`。
- 所有写接口接受可选 `request_token`：同一复核重试命中幂等键时原样返回首次响应，不产生第二套数据、不重复推进修订；不传时按目标证据指纹派生幂等键。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
