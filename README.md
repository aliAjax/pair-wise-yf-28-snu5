# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、入组中方案修订（双人确认）、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `POST /api/trials/{id}/amendments`：协调员提交方案修订（新分组、新分层因素、区组长度、种子、方案版本）；提交后入组立即暂停，响应为修订号 1 起的修订记录。
- `POST /api/amendments/{id}/confirm`：监查员确认，须两位不同监查员各确认一次；第二人同意后修订生效。
- `POST /api/amendments/{id}/reject`：监查员驳回（可附原因），驳回后入组按原规则恢复，协调员可重新提交。
- `GET /api/trials/{id}/summary`：中心级汇总、当前生效随机规则、各次修订的状态与生效时间、审计记录。

### 入组中方案修订规则

- 修订提交后到两位监查员确认完成前，新入组一律拒绝（`enrollment_paused`）；已入组受试者编号的幂等重放不受影响。
- 第二位监查员同意后修订即时生效（`effective_at`），下一例入组按新分组与分层规则随机；生效前的入组仍走旧规则。
- 早先入组的受试者保留原分组与原分层，参与者记录带 `revision_no`（0 为原始方案）。
- 随机号按 `revision_no` 隔离：旧规则下已预生成但未使用的随机号不会发给新规则下的受试者。
- 修订一生效即锁定，不能再次修订；有待确认的修订时也不能重复提交。

随机表按“试验种子 + 修订号 + 中心 + 分层因素”确定性生成，每个区组长度为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。旧数据库启动时自动补 `revision_no` 列并建修订表。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
