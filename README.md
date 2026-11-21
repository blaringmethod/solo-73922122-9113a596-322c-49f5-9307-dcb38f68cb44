# 河流水质站日 CSV 发布服务

面向科研核验的不可改原件发布 API：原件原字节存共享卷，SQLite 只存目录与摘要，
以 SHA-256 固定深度 Merkle 树出具检查点、单件纳入证明与检查点追加证明，
并提供完全离线的验证函数/CLI。两个实例挂载同一卷并发发布，不分叉、不覆盖。

科研组本地目录与留存检查点对不上时，可用**目录对账 API** 按历史检查点
从根摘要逐层下钻差异区间，单条差异返回检查点承诺的目录字段，
再凭离线函数核实缺单与错单的完整清单；全程只交换摘要与目录信息。

## 1. 目录结构

```
app/
  config.py     运行配置（DATA_DIR、1 MiB、128 件、树深度 7）
  storage.py    原件字节存储：内容寻址 + 临时文件 fsync + 原子改名
  db.py         SQLite 持久层：WAL、BEGIN IMMEDIATE 串行化、启动自检、对账查询
  merkle.py     Merkle 树纯函数：根、子树摘要、纳入证明、追加证明、离线验证
  reconcile.py  目录对账客户端协议：从根摘要下钻差异区间，汇总对账单
  verify.py     离线核验函数与 CLI（python -m app.verify ...）
  schemas.py    HTTP 响应模型
  main.py       FastAPI 路由（阻塞调用均走线程池）
tests/          43 项 pytest 用例（算法、API、对账、故障路径）
scripts/
  demo.sh                 HTTP 端到端演示（含目录对账与离线清单核验）
  dual_instance_check.py  双进程共卷并发/故障转移/跨实例对账集成检查
Dockerfile
docker-compose.yml        两个实例共卷，端口 8001/8002
```

## 2. 启动

### Docker Compose（交付目标形态）

```bash
docker compose up --build
# 实例 A：http://localhost:8001/docs
# 实例 B：http://localhost:8002/docs
```

两实例镜像相同、挂载同一命名卷 `shared-data`（`/data`），
`MAX_FILE_BYTES=1048576`、`MAX_RELEASES=128`（对应 Merkle 深度 7，128 个叶子槽）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
# 打开 http://localhost:8000/docs
```

## 3. API 一览（/docs 可交互演示）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/releases` | 表单 `station_id`、`sample_date`(YYYY-MM-DD)、`order_no`、`file`；同单同输入重试复用（`reused=true`），异输入 409，超 1 MiB 返回 413，超 128 件返回 409，写盘失败返回 507 |
| GET | `/releases` | 全部发布目录记录（不含字节） |
| GET | `/releases/{station}/{date}/{order}` | 单条目录记录 |
| GET | `/releases/{station}/{date}/{order}/download` | 下载已提交原件，文件名带站日单号 |
| GET | `/releases/{station}/{date}/{order}/inclusion-proof` | 单件纳入证明（恒 7 步路径） |
| GET | `/checkpoints` | 全部检查点（含 0 号空树检查点） |
| GET | `/checkpoints/latest` | 最新检查点（条数 + Merkle 根） |
| GET | `/checkpoints/{seq}` | 任意旧检查点 |
| GET | `/checkpoints/{old_seq}/append-proof` | 旧检查点 → 最新检查点的追加证明 |
| POST | `/reconcile` | 目录对账：按指定历史检查点核对发布前缀。请求为一批 Merkle 区间 `{start, height, local_root}`（第一轮从根区间 `{0, 树深度, 本地根摘要}` 开始）；逐区间返回 `match`（摘要一致）/ `diverged`（附服务器侧摘要，按两个子区间继续核对）/ `leaf`（单条不一致，附检查点承诺的目录字段）。本地缺单按空叶处理；检查点不存在 404，区间未对齐/越界 422 |
| GET | `/health` | 启动自检结果（缺件/摘要不符时 `degraded`，并拒发证明与下载） |

## 4. 核验模型（科研组离线使用）

### 叶子与树

* 叶子载荷为带版本号的规范化 JSON（键排序、无空白）：
  `{v, station_id, sample_date, order_no, blob_sha256, size}`，
  叶子哈希 = `SHA256(0x00 || 载荷)`；内部节点 = `SHA256(0x01 || left || right)`，域分隔防二阶碰撞。
* 固定深度 7 的完整二叉树，未用槽位以公开常量 `EMPTY_LEAF` 占位：
  追加只改变新叶子到根的路径，旧证明不会因树“长高”失效。
* **纳入证明**：7 个兄弟哈希（O(log n)），每步带 `bit` 与下标逐位绑定，防重放到其他下标。
* **追加证明**：把旧前缀与新增区间分别平铺为对齐的完美子树，条目数 ≤ 2×深度（实测 128 件规模最多 8 条）。

### 离线核验（无需服务端、无需网络）

```bash
# 单件：凭留存的纳入证明 + 待验原件，核验站日归属与检查点纳入
python -m app.verify inclusion --proof inclusion.json --file 原件.csv

# 历史前缀：凭两个留存检查点 + 追加证明，核验仅追加关系
python -m app.verify append --proof append.json \
  --old-checkpoint cp-old.json --new-checkpoint cp-new.json

# 目录对账：凭本地目录 + 对账单 + 留存检查点，核实缺单与错单完整清单
python -m app.verify reconcile --local 本地目录.json \
  --reconciliation 对账单.json --checkpoint 留存检查点.json
```

也可直接导入纯函数：`app.verify.verify_release_bytes`、`app.verify.verify_append_proof`、
`app.verify.verify_reconciliation`。
篡改原件、替换历史条目、伪造检查点根、证明重放到其他下标均会被拒绝（退出码 1）。

### 目录对账协议（找出该补的发布单）

科研组留存某历史检查点 `(seq, root)` 后，按如下协议核对本地目录：

1. 本地目录按下标展开为固定深度树的叶子序列，**本地缺单的位置以
   `EMPTY_LEAF` 占位**，`seq` 之后的槽位同为空叶；
2. 从根区间 `(start=0, height=树深度)` 开始，把本地算出的区间摘要
   `POST /reconcile` 给任一实例；
3. 只继续核对摘要不一致的区间：`diverged` 区间分裂为左右两个子区间进入
   下一轮，`match` 区间剪枝；缩小到单条（`leaf`）时，响应携带检查点承诺的
   目录字段 `{v, station_id, sample_date, order_no, blob_sha256, size}`；
4. 各轮响应的 `results` 合并为对账单，交给离线函数核实。

* **快照一致**：服务端在单个只读事务内只读 `id ≤ checkpoint_seq` 的历史前缀
  （发布只增不改，该前缀不可变），对账期间的新发布不改变这一轮的结果；
  多轮之间发生新发布同样不影响（见测试）。
* **只交换摘要与目录**：请求只含区间摘要，响应只含区间摘要与单条目录字段，
  原件字节不参与对账；对账只读数据库目录，卷上缺件（degraded）时仍可用于排查。
* **通信量**：往返轮数 ≤ 树深度 + 1（根区间 + 每层一次，默认配置下最多 8 轮），
  每轮区间数随差异个数增长（k 处差异约 `2k·depth` 个区间结果），与目录总条数无关。

离线函数 `app.verify.verify_reconciliation(local_records, reconciliation,
checkpoint)` 的通过与清单完整性由同一条不变量保证：

> 用本地叶子（缺单按空叶）与对账单提供的目录字段重建出的树根，
> 必须等于留存检查点根；且每个 `diverged` 区间的两个子区间都必须有后续结果。

因此**只要漏掉一个有差异的区间**——未下钻到底、被谎报为 `match`、或目录
字段被篡改——重建根就与留存根不同（或结构校验失败），清单即判不通过，
即使已取回的记录全部正确。通过时输出：

* `missing`：缺单清单（检查点承诺而本地没有的目录字段，即该补的发布单）；
* `mismatched`：错单清单（本地记录与检查点承诺不符的对照）。

客户端参考实现为 `app.reconcile.run_reconciliation`（`scripts/demo.sh`
第 9–10 步演示完整流程）。

## 5. 崩溃安全与并发不变量

1. **先落盘后入库**：原件写临时文件 → `fsync` → `os.replace` 原子改名 → `fsync` 目录，
   成功后才开数据库事务；写盘失败无库记录、临时文件清理、可原样重试。
2. **串行化发布**：每个发布事务 `BEGIN IMMEDIATE`，双实例写操作被 SQLite 锁串行化，
   后到者等待后重检唯一键 `(station_id, sample_date, order_no)`；无 UPDATE/DELETE 路径。
3. **只增不改**：同键同字节复用、同键异字节 409；检查点与记录均只追加。
4. **启动自检**：重算全部叶子与根，逐件校验卷上原件摘要并检查序号稠密、检查点连续；
   发现缺件即 `degraded`，拒绝发布/证明/下载，绝不输出缺件记录。
5. **重启有效**：历史检查点根由固定深度树确定，重启后逐点复算一致，留存证明持续有效。
6. 阻塞式磁盘/DB 调用均在任意线程池执行，锁等待不阻塞事件循环；
   写锁冲突在 `busy_timeout` 之外另有 3 次有限重试。

## 6. 测试结果（本环境实测）

* `pytest tests/`：**45 项全部通过**
  - Merkle：根稳定性、子树摘要与全空捷径、纳入证明、全部前缀追加证明、
    篡改/重放/重叠/缺口拒绝、平铺覆盖性质；
  - API：发布、幂等复用、409 冲突、413/422 限额与校验、128 上限、下载字节一致、
    两类证明与离线核验；
  - 目录对账：根一致短路、缺单/错单/混合下钻、空目录全量补单、按旧检查点与
    0 号空树检查点对账、两轮之间与对账期间新发布不改变结果、degraded 状态下
    对账可用、404/422 校验、大写 hex 归一化、离线 CLI；
  - 对账安全性质：漏掉差异区间（未下钻、谎报一致、篡改目录字段/服务器摘要、
    对账单绑定错误检查点、重复/未对齐区间）一律判不通过；
  - 故障：写盘失败不留记录且可重试、缺件自检与 503 拒服、孤立临时/原件无害、
    已引用原件被改可检出、重启后检查点可复算。
* `scripts/dual_instance_check.py`（两个独立 uvicorn 进程共卷）：**20 项全部通过**，
  覆盖并发同单（一写一复用）、10 件并发异单序号 1..11 稠密、跨实例取件/检查点/证明、
  跨实例目录对账与离线清单核实、两实例对账结果一致、对账期间新发布不影响本轮、
  第三进程挂同卷的故障转移视角与继续追加。
* 手工冒烟（默认 1 MiB / 128 件 / 深度 7）：1 MiB+1 返回 413、恰 1 MiB 成功、
  发满 128 件后第 129 件 409、检查点 0..128 连续、离线 CLI 核验通过、篡改原件被拒；
  `scripts/demo.sh` 端到端（含第 9–10 步对账演示）在本环境实跑通过；
  生产配置对账冒烟 12 项通过：15 处分散差异（10 缺 5 错）8 轮往返完成下钻、
  清单精确匹配、空本地目录对 128 号检查点得出 128 件全量缺单清单、
  容量满后同检查点对账结果不变。
* 证明规模实测：纳入证明恒 7 步；追加证明最大条目随条数 1→2→3→…→8（对数增长）；
  对账在 2 处差异（1 缺 1 错）下共交换 15 个区间结果，15 处差异下 73 个。

## 7. 剩余工作与已知限制（据实说明）

1. **本环境无 Docker 守护进程**，`docker compose` 未实际起容器验证；已用双 uvicorn
   进程共享同一 `DATA_DIR` 做等价并发/故障转移/跨实例对账测试。Dockerfile 与
   compose 配置已交付，需在具备 Docker 的环境执行一次 `docker compose up --build` 复核。
2. 沙箱以 seccomp 禁止 `kill` 系统调用，集成脚本无法终止演示进程，改为随机端口 +
   每次独立临时目录；脚本中的“重启”以第三进程挂同卷模拟（故障转移语义等价）。
3. 128 件上限在实现上是固定深度树的叶子槽位数（2 的幂，可经 `MAX_RELEASES` 调整）；
   非 2 的幂配置会在启动时直接报错。
4. 未引入鉴权/传输加密/TLS，定位为内网或网关后的发布服务；对外暴露需前置网关。
5. 共享卷自身的备份与异地容灾未包含在本服务内（卷快照/备份应由基础设施层提供）；
   服务能检出卷上缺件与篡改，但不会自动恢复字节。
6. 原件类型未强制 CSV 内容嗅探，仅做大小与空文件校验，避免对业务 CSV 方言做过强假设。
7. 目录对账会向调用方暴露“哪些下标存在差异”及差异处的目录字段（不含原件字节），
   与 `GET /releases` 公开目录的信息量一致；调用方需先经 `GET /` 的
   `limits.tree_depth` 获知树深度（客户端参考实现已演示）。
8. 离线核验只承认“从根区间出发、结构完整”的对账单；对账单中无法从根到达的
   多余条目不影响判定（重建不引用它们），但客户端实现仍应只保留下钻路径上的结果。
