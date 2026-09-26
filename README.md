# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、运行中方案修订（分层标准调整）、入组暂停、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

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
- `GET /api/trials/{id}/summary`：中心级汇总、修订状态和审计记录。

## 运行中方案修订（分层标准调整）

方案锁死后入组期内只允许受控修订，由分层随机服务保证规则切换的严格边界：

- `POST /api/trials/{id}/amendments`：协调员提交新分组与新分层因素（附新版本号）。提交后**入组立即暂停**，期间所有入组请求以 `409 enrollment_paused_amendment_pending` 拒绝；同一试验同时只能有一份待确认修订。
- `POST /api/protocol-amendments/{id}/confirm`：两位监查员**分别**确认（同一人不能确认两次，返回 `409 distinct_confirmer_required`）；第二人确认的瞬间修订生效，下一例入组起按新规则随机。
- `POST /api/protocol-amendments/{id}/reject`：监查员驳回（可附理由），入组恢复旧规则，之后可重新提交，修订序号继续递增。
- `GET /api/trials/{id}/amendments`：列出各次修订的状态（待确认/已生效/已驳回）、两位确认人和**生效时间**；中心用户看不到修订后的分组名称。

边界保证：

1. **生效前仍按旧规则**：待确认期间不入组；生效瞬间之前入组的受试者保留原分组与原分层记录。
2. **旧预留随机号不跨规则发放**：分层表与随机号均带修订版本号；新修订的随机表以 `种子:r{修订号}:分层键:区组` 独立生成（修订 0 沿用原始种子格式，保证升级前随机表逐位不变）。早先预留但未使用的随机号留在原版本池内，永不会发给新规则下的受试者。
3. **生效后不可再改**：已有生效修订后再次提交返回 `409 amendment_locked`；区组长度与种子不随修订改变。
4. 页面顶部横幅展示入组暂停/修订已锁定状态，表格展示每次修订的状态和生效时间，并按版本统计预留未用随机号数。

随机表按“试验种子 + [修订号] + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
