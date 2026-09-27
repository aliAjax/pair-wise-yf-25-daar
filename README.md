# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 模块划分

- `app.py`：评审主流程与 HTTP 层。
- `revision_rules.py`：返修规则（7 天窗口期、可返修决定类型、逐条回复校验），规则单独维护。
- `version_archive.py`：论文版本存档（不可变快照写入与查询），与评审流程解耦。
- `web/author.html`：作者页面（`/author`），返修交付与版本查询与演示首页分开维护。

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定；返修轮须等所有原评审人提交新版意见后才能改判。
- `POST /api/papers/{id}/revision`：大修/小修决定后 7 天内作者提交修订稿与逐条回复，论文回到评审中。
- `POST /api/papers/{id}/revision-reviews`：原评审人针对修订版提交一份新意见，每人一份。
- `GET /api/papers/{id}/revision`：返修状态、截止时间与新版意见进度（非主席视角评审人匿名）。
- `GET /api/papers/{id}/versions`：版本存档，新旧版本均可查。
- `GET /api/papers/{id}/history`：审计历史。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

返修交付不变量：仅大修/小修（`major_revision`/`minor_revision`）决定后可返修；作者须在决定后 7 天内提交一次修订稿与逐条回复，逾期或重复提交返回 409；每次返修生成新的版本快照，旧版本永久可查；返修时论文回到 `under_review`；原评审意见只留档，所有原评审人各提交一份针对新版的意见后，主席才能改判；逾期未返修或返修意见未交齐时主席无法改判。
