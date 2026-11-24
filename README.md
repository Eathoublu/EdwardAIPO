# Edward — 会自己上班的 AI 项目 Owner

![EDWARD.jpg](EDWARD.jpg)

别的 Agent 帮你干活，Edward 替你**扛结果**。

它不蹲在聊天框里等你开口，而是一个自带闹钟的项目负责人：到点自己上班、读目标、推计划、拉团队、盯 KPI、做看板、写完报告，再给自己定下一次闹钟下班。你只需要在它汇报后回一句话，剩下的交给它。

## 它和普通 Agent 差在哪

- 普通 Agent：对**过程**负责。你问一句，它答一句。
- Edward：对**结果**负责。它盯着自己的 KPI，没做完就不敢说做完了。

它不是工具人，是 project owner。会自己判断、自己决策、自己扛指标，小事不烦你，大事才找你拍板——抓大放小，这才是领导想看到的。

## 它上班都忙些什么

Edward 的脑子分成三块地，边界清清楚楚：

- **法典域 `codex/`**：你定的规则和红线，它只读、只遵守，不许自己改。
- **执行域 `execution/`**：它的工作台。目标、计划、待办、团队、看板、班次报告全在这。
- **知识域 `knowledge/`**：项目知识库，越用越厚，经验沉淀下来不掉线。

每个班次它还会：

- **组队**：按 `team.yaml` 拆角色、派活儿给 subagent，而不是一个人闷头包圆。
- **盯盘**：把每个 KPI 变成固定采集脚本，随时刷出真实数字，杜绝拍脑袋。
- **记账**：跨班次待办清清楚楚，逾期、今日到期优先办。
- **汇报**：写这一班的战报，列出阻塞和要你拍板的事，再定好下次几点上班。
- **建看板**：给你一个随时能打开的网页，指标、时间线、待办、团队一屏看全。

## 三分钟装上

把 Edward 装进你的项目（会落到项目的 `.opencode/` 里）：

**图形界面**：双击 `dist/Edward Installer.app`，选项目目录，点「安装 Edward」。

**命令行**：

```bash
python3 dist/edward-installer.pyz --cli --target /path/to/你的项目 --init --yes
```

装完它就会自己跑起来：

```bash
/path/to/你的项目/.opencode/bin/edward status        # 看看它醒没醒
/path/to/你的项目/.opencode/bin/edward serve --daemon # 启动后台调度 + 看板
```

看板打开就是：http://127.0.0.1:8765/

常用小扳手：`init` `serve` `ui` `schedule` `status` `collect` `dashboard` `todo` `team` `run-now` `pause` `resume` `stop`——记不住也没事，它自己会用。

## 人类只需要做一件事

当好领导，别抢活。它汇报完，你把意见写进一句话就行：

- 想留痕：写 `edward/execution/report-reply/<run-id>.md`
- 想省事：点开看板，在回复框里敲两行，回车

它下次上班会把你所有的追加回复按时间读完再开工。你越敢放权，它越像样。

## 现在就上车

还在把 AI 当"打字员"用？让它升级成"项目负责人"吧。

**Edward AIPO** —— 你负责定方向，它负责把结果端到你面前。装一个，睡个安稳觉，醒来就有战报。

> 版本号见 `release.yaml`；重新构建安装包运行 `./installer/build.sh`。
