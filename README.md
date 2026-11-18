# 反欺诈特征计算与在线服务平台

面向网络支付机构的特征平台（Feature Store）：风控分析师在特征目录中登记交易行为特征，
**离线训练集与实时评分读取同一计算口径**。平台持续维护流式窗口结果，迟到事件只触发受影响
账户与窗口的修订，用同一特征定义重放历史输入生成训练值。

在冻结的训练样本追溯与特征版本之上，平台提供**可证明完整性的复现包（复现清单）与保留治理**：
模型风险管理部可对同一份历史输入复现"某笔交易为何进入人工复核"，并在原文过期后仍能证明当时
使用过哪些输入及其摘要——但不会把不可复现的过期记录宣称为已复现。

技术栈：Python 3.12 · FastAPI · PostgreSQL · React (Vite)

## 核心设计

### 口径一致性如何保证

- **同一个计算函数**：`backend/app/services/compute.py` 中的 `compute_feature_value` 同时服务
  于在线查询、检查点物化与训练重放，离线/在线不存在两套实现。
- **流式窗口状态**：计算作业为每个（特征版本 × 账户）维护窗口贡献状态 `window_entries`
  （已去重、按事件时间索引、只含该特征过滤后的输入），在线读取是对该状态的索引化区间聚合，
  不在请求时扫描原始交易表；事件边界处同时物化检查点 `checkpoints`。
- **窗口语义**：`(as_of − W, as_of]` 左开右闭，全程事件时间（timestamptz），无日历日截断，
  跨日窗口天然正确。窗口归属是事件时间的纯函数，与到达顺序无关；相同事件时间的事件按
  稳定键 `(event_time, event_id)` 处理，检查点物化顺序确定。
- **去重**：`(source, event_id)` 唯一约束。重复交易回调被确认但不重复应用，窗口计数不变，
  重复次数计入 `duplicate_attempts` 并在计算状态页可见。
- **迟到修订**：事件时间落后于该（特征 × 账户）水位即为迟到。迟到事件只重算
  `[event_time, event_time + W)` 范围内的检查点，且每次值变更写入 `revisions` 审计
  （旧值 → 新值、触发事件、检查点 as_of），其他账户与其他窗口不受影响。
- **版本钉住**：发布 = 新建不可变的特征集版本 + 单行原子切换在线指针。每个在线请求只在
  开始时解析一次版本，因此发布中途的请求要么读到完整旧版本、要么读到完整新版本，
  绝不会把两个版本的字段拼成一条评分输入。也可显式 `set_version=` 钉住历史版本。
- **缺失不伪装**：上游账户源不可用（或某账户无快照）时，账户类特征返回
  `status=unavailable, value=null` 并附来源状态与最近成功时间，绝不返回 0。
  （空窗口的 COUNT/SUM 返回 0 是真实度量，与缺失不同；空窗口 AVG/MIN/MAX 返回 null。）
- **训练追溯**：训练样本记录决策时点；追溯接口按同口径重放特征值，并列出每个值由哪些
  输入记录（交易/标签/账户快照）算出。拒付与人工核查标签延迟到达后，样本标签自动
  由 pending 变为 confirmed，标签类窗口特征同步修订。

### 复现包与保留治理

**冻结 → 生成 → 校验 → 受控重放**，每一步的状态都可查询、可审计：

- **模型批次冻结**：`ModelBatch` 钉住模型版本（含确定性评分规格与摘要）、特征集版本、样本集合，
  并记录输入摘要与计算快照。冻结后不可变；复现包只能从冻结批次生成。
- **复现清单内容**：数据范围（实体、决策时点区间、各类别记录数与类别策略）、事件顺序（全部输入的
  确定性排序）、字段可用性、逐样本特征值与特征定义摘要、标签成熟度（是否在决策后补达）、
  冻结模型评分、执行环境指纹（Python 版本 / 计算引擎 / 摘要算法等）与清单完整性校验和。
- **六类状态持有者**：数据保留（策略版本 + 清理作业）、特征计算（特征定义摘要 + 引擎版本 +
  计算快照）、模型批次、证据封装（清单与摘要）、权限审批、审计查询各自独立记录状态；
  `/replay-packages/{id}/dependencies` 让审计员从一份复现包查到全部依赖版本。
- **按类别可解释的保留策略**（版本化、不可变；复现包在生成启动时冻结所用策略快照）：
  - 交易明细：原文 30 天 + **加密副本 365 天**（AES-256-GCM，按密钥版本加密）。原文过期后可凭
    明文审批在受控环境解密复算，兼顾拒付争议与模型复核。
  - 拒付/核查标签：法定留档类别，原文 180 天、加密副本 730 天；法定期间内不因普通保留到期而销毁。
  - 账户资料：**仅保留可验证摘要与派生特征**（数据最小化），不额外保存加密副本。原文过期后仍能
    证明使用过哪些输入及其摘要，但**有意不支持逐字段复算**。
- **三种必须区分的结论**：`原文不可用`（plaintext_unavailable）、`证据链损坏`
  （evidence_chain_broken）、`可在受控环境重放`（replayable_plaintext / replayable_encrypted）。
  校验会列出受影响评分，绝不把不可复现记录标记为已复现。
- **密钥生命周期**：轮换（旧包仍用其封存时的密钥版本解密）、撤销（加密副本进入锁定态，校验归为
  原文不可用）、恢复（重新可解密，回到可重放）。每类证据记录都记录所用密钥版本与状态。
- **摘要算法迁移**：新策略可切换摘要算法；旧包按封存时算法验证并在校验结果中标注迁移状态，不会
  因策略迁移被误判为证据链损坏。
- **来源补传修改**：合法补传会改变当前规范记录摘要，但已封存证据摘要不变；校验将其报告为证据链
  损坏并列出受影响评分，而不是静默用新数据重放。
- **最小化披露与审批分离**：清单只含摘要与派生特征；明文查看需独立的 `plaintext` 审批，
  受控重放需 `replay` 审批；管理员审批、申请人不可自批。

### 数据流

```
交易回调 / 拒付标签 / 账户日快照
        │  POST /api/ingest/*（幂等去重）
        ▼
processed_events（去重日志）──► window_entries（流式窗口状态）
                                  │        ▲ 迟到事件只修订受影响范围
                                  ▼        │
                            checkpoints（事件时点物化值）──► revisions（修订审计）
                                  │
        在线评分 ◄────────────────┘  训练重放 / 数据集构建（同一计算函数）
```

## 目录结构

```
backend/            FastAPI 后端
  app/models.py       数据模型（目录/事件/窗口状态/检查点/修订/样本）
  app/services/       compute(窗口引擎) ingestion serving training features simulation
  app/services/governance.py  保留策略/密钥/摘要/清理引擎
  app/services/replay.py      复现包生成、校验、受控重放、审批
  app/routers/        REST API（含 /api/replay-packages、/api/retention、/api/keys）
  tests/              验收测试（pytest，20 项）
frontend/           React 前端（特征目录/计算状态/在线查询/训练样本/复现与留档/数据模拟）
scripts/verify_e2e.py  端到端验收脚本（针对运行中的服务，29 项检查）
scripts/dev_start.sh   本地一键启动（后端 + 前端 dev server）
docker-compose.yml  一键启动（db + backend + frontend）
```

## 本地启动

前置：PostgreSQL（127.0.0.1:5432，库/用户/密码均为 `ccdev`）、Python 3.12、Node 22。

```bash
# 后端（首次自动建表并播种：测试账号 + 默认特征集 fraud_scoring v1）
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（新终端，dev server 代理 /api 到 8000）
cd frontend
npm install
npm run dev        # http://localhost:5173
```

也可以 `bash scripts/dev_start.sh` 一次拉起两者。API 文档：http://127.0.0.1:8000/docs

## Docker 一键启动

```bash
docker compose up --build
# 前端 http://localhost:8080 ，后端 http://localhost:8000/docs
```

compose 使用独立的 `featuredb/featureapp` 数据库账号（非开发容器账号），首次启动自动建表播种。

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `analyst` | `analyst123` | 风控分析师 |
| `admin` | `admin123` | 管理员 |

## 验证方式

### 1. 单元/验收测试（pytest，20 项）

```bash
cd backend && .venv/bin/python -m pytest tests -v
```

覆盖：重复回调去重、相同事件时间稳定键与半开窗口边界、跨日窗口、迟到修订范围
（仅受影响账户与窗口 + 审计值）、延迟拒付标签、发布中途不混版本（多线程并发）、
离线/在线同口径（含迟到后再验证）、上游缺失不伪装零值、样本输入记录追溯、鉴权；
以及复现与保留治理：清单内容与完整性、同模型多次重放一致、重放/明文审批分离、
保留期届满后仅存摘要且不可复现、密钥撤销锁定与恢复后可重放、密钥轮换后旧包可解密、
摘要算法迁移兼容、来源补传导致证据链损坏并列出受影响评分、清单生成中途策略更新仍用启动快照、
法定留档类别在普通保留到期后仍保留加密副本、审计查询状态。

### 2. 端到端验收脚本（29 项检查，需后端已启动）

```bash
/opt/python-3.12/bin/python3 scripts/verify_e2e.py
```

按验收场景走完整流程：重复回调 → 跨日窗口 → 迟到修订范围 → 延迟拒付 →
发布中途版本完整 → 离线样本与在线读取一致 → 账户源中断返回状态而非零值 →
批次冻结与复现清单完整性 → 未审批重放被拒 → 同模型多次重放结果一致 → 依赖版本可查 →
保留期届满仅存摘要且不宣称复现 → 清单生成中途策略更新仍用启动快照 → 来源补传证据链损坏 →
密钥撤销/恢复后的可重放性切换。

### 3. 页面手工验证

1. 「数据模拟」按 1→2→3 生成账户快照、交易流（含重复回调与迟到事件）、延迟标签。
2. 「计算状态」查看水位、迟到计数、重复回调数与修订日志（旧值→新值）。
3. 「在线查询」读取某账户特征；切换 as_of 观察窗口变化。
4. 「训练样本」以某笔交易时间创建样本并点「追溯」，与同一 as_of 的在线值逐项对比。
5. 「数据模拟 → 模拟账户源中断」后再次在线查询：账户类特征显示 unavailable 而非 0。
6. 「特征目录」勾选特征发布新版本，发布期间在线查询始终读取完整单一版本。
7. 「复现与留档」：冻结批次 → 生成复现包 → 完整性校验 → 申请重放权限（管理员通过）→ 受控重放；
   连续重放两次查看结果摘要一致。
8. 同页「数据保留策略与到期清理」：以未来 as_of 执行清理 → 再次校验，观察"原文不可用/仅摘要"
   与"不可复算"结论；发布新策略版本时选「生成（暂停封存）」可演示中途策略更新使用启动快照。
9. 「证据密钥」撤销后校验显示加密副本锁定（原文不可用），恢复后重新可重放；「模拟来源补传修改」
   后校验显示证据链损坏并列出受影响评分。

## 实际执行的验证结论

- `pytest tests`：20 项全部通过（测试库 `ccdev_test`，每用例重建 schema）。
- `scripts/verify_e2e.py`：29 项检查全部 PASS（针对本地运行的后端 + 独立 `ccdev_e2e` 库）。
- 复现与留档接口在独立库上经 HTTP 实测：模拟账户/交易/标签 → 冻结批次 → 生成复现包 →
  校验（replayable_plaintext，6 条证据）→ 审批后连续两次重放结果摘要一致（consistent）。
- `/docs` OpenAPI 文档可打开（44 个路径，其中 16 个为复现/保留/密钥相关）。
- 前端 `npm run build` 构建通过；vite dev server 页面与 `/api` 代理实测可用。
- 后端 uvicorn 启动后，模拟数据（账户/交易/标签）、在线查询、修订日志、
  计算状态、样本追溯、数据集构建等接口均经 curl 实测。
- 当前容器无 docker 命令，`docker-compose.yml` 仅做了 YAML 静态校验，未实际构建镜像；
  镜像所需依赖（`cryptography==44.0.0`）已加入 `backend/requirements.txt`。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/auth/login | 登录获取 token |
| GET/POST | /api/features | 特征目录查询 / 新建特征（版本） |
| GET | /api/feature-sets | 特征集版本与当前指针 |
| POST | /api/feature-sets/{name}/publish | 原子发布新版本 |
| POST | /api/ingest/transactions | 交易回调（幂等去重、迟到检测） |
| POST | /api/ingest/labels | 拒付/核查结果（延迟到达） |
| POST | /api/ingest/account-profiles | 账户日快照 |
| GET | /api/online/features | 在线特征查询（钉住完整版本） |
| GET | /api/compute/status、/watermarks、/revisions | 计算状态与修订审计 |
| POST/GET | /api/training/samples | 训练样本 |
| GET | /api/training/samples/{id}/trace | 样本追溯（决策时点/特征值/输入记录） |
| POST/GET | /api/training/datasets | 训练数据集构建与读取 |
| POST | /api/simulate/* | 本地模拟数据与故障演练 |
| GET | /api/sources | 上游来源状态 |
| GET/POST | /api/models | 模型版本（确定性评分规格 + 摘要） |
| GET/POST | /api/model-batches | 冻结批次；`POST /{id}/freeze` 冻结 |
| GET/POST | /api/replay-packages | 复现包列表 / 从冻结批次生成（可 `pause_before_seal`） |
| POST | /api/replay-packages/{id}/seal | 封存（校验并使用启动时策略快照） |
| GET | /api/replay-packages/{id}/verify | 完整性校验：分类、字段可用性、受影响评分 |
| POST | /api/replay-packages/{id}/replay | 受控重放（需已批准的 replay 审批） |
| GET | /api/replay-packages/{id}/runs | 多次重放记录与结果摘要 |
| GET | /api/replay-packages/{id}/manifest | 复现清单（仅摘要与派生特征） |
| GET | /api/replay-packages/{id}/dependencies | 六类依赖持有者的状态与版本 |
| POST | /api/replay-packages/{id}/reveal | 明文查看（需已批准的 plaintext 审批） |
| GET/POST | /api/replay-packages/{id}/approvals | 审批申请；`POST /api/approvals/{id}/decide` 管理员审批 |
| GET/POST | /api/retention/policies | 版本化保留策略 |
| GET/POST | /api/retention/sweeps | 保留期届满清理（原文过期 / 副本销毁） |
| GET | /api/keys；POST /api/keys/rotate、/{id}/revoke、/{id}/restore | 证据密钥生命周期 |
| GET/POST | /api/source-corrections | 来源补传修正（演练证据链损坏） |
