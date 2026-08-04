# AionsHome

FastAPI 后端 + 原生 JS 前端的个人 AI 伴侣项目。后端在 `aion-chat/`。

## 多窗口并行开发

用户经常同时开多个 Claude Code 窗口修不同的 bug。功能按文件天然隔离
（sleep / chat / music / chatroom 各一套），一般不会撞。但有三件事必须先打招呼：

### 1. 枢纽文件 —— 动之前先问用户

任何功能都可能顺手改到这些，两个窗口同时改会互相覆盖：

    config.py            ai_providers.py       tts.py
    routes/settings.py   static/settings.html  main.py
    static/chat.js

`static/chat.html:313` 的 `?v=` 尤其危险。改 `chat.js` 必须同步 bump 它，
但两个窗口同时 bump 同一行，后一个会把前一个的 chat.js 改动锁在旧缓存里，
F5 拿到旧代码，很难排查。`static/sleep.html:593` 同理。

### 2. 重启后端 —— 先问用户

`main.py` 硬编码 `port=8080` 且无 `--reload`，改 `.py` 不重启就是旧代码。
端口只有一个，重启会掀掉别人正在测的进程。
纯前端改动（.js/.css/.html）不用重启，F5 即可。

### 3. `aion-chat/data/` —— 只在一个窗口写

settings.json 和各种 jsonl 都在这，整目录 gitignore，
并发写 git 看不见，出问题没有 diff 可查。

### 优先 Edit，避免 Write

Edit 做字符串替换，文件被别的窗口改过会直接报错。
Write 整文件覆盖，会静默吃掉别人的改动。改动尽量走 Edit。

## Git

功能验证 OK 后立刻按主题 commit，不等积攒（并行时干净的树打架成本低很多）。
`git commit` 由用户决定时机，不要自动提交。

commit 时必须按文件名精确 `git add <file>...`，禁止 `git add -A` / `git add .`——
别的窗口未提交的改动会被一起扫进来（2026-08-04 音乐修复就这样被另一窗口的
未读功能 commit 卷走了）。同一文件混了两个主题的改动时，用 `git add -p` 按 hunk 挑。
