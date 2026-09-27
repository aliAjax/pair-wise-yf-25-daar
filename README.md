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
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

### 返修交付（大修/小修决定后）

- `POST /api/papers/{id}/revision`：作者在决定后 **7 天内**提交修订稿（title/abstract）与逐条回复（response），一次性原子操作；论文回到 `under_review`，修订稿存为新版本。
- `GET /api/papers/{id}/revision`：返修状态（截止时间、新版号、各原评审人新版意见完成情况、主席是否可改判）。
- `GET /api/papers/{id}/versions`：版本存档，初投与各次返修稿均可查；评审人为双盲元数据视图。
- `POST /api/papers/{id}/revision-reviews`：原评审人各针对新版提交一份意见（原有意见只留档）。
- `POST /api/papers/{id}/redecide`：全部原评审人完成新版意见后，主席改判（决定进入下一轮）。
- 作者页面独立维护于 <http://127.0.0.1:8101/revision.html>。

代码按关注点分模块维护：`revision_rules.py`（返修规则、复审、改判）、`version_archive.py`（版本存档与旧库迁移）、`web/revision.html`（作者页面），`app.py` 通过 Mixin 组合二者。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。返修额外保证：只对 `major_revision`/`minor_revision` 决定开放且窗口为 7 天（逾期 409）；返修稿、每位原评审人的新版意见、主席改判都只能提交一次（重复 409）；全部原评审人完成新版意见前主席不能改判（409）；旧版本永不覆盖，可通过版本存档持续查询。
