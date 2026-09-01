# AGENTS.md — Sigilicon Python package

适用于本目录及全部子目录。Sigilicon 是可跨项目复用的 EDA execution/evidence kernel；
只拥有 project composition、operation planning、受管执行、artifact/result identity、公共
安全检查，以及真实复用的 tool backend。具体 IP（含层次化 composite IP）、PDK、target、
qualification 门槛和仓库布局由调用项目拥有。标准 ASIC、模拟/混合信号和 custom layout
保持各自的领域 contract，通过同一个 Project -> ExecutionPlan -> RunResult 生命周期组合，
不得为统一入口而拍平领域 payload。

- 本仓库只维护 `AGENTS.md` 作为 agent 术语、ownership 和工作规则入口；不得新增
  `CONTEXT.md`、`docs/` 或平行架构说明。架构事实只由 source、类型和测试表达。
- 公共执行词汇固定为 `Project`、`Operation`、`Step`、`Backend`、`ExecutionPlan`、
  `RunResult` 和 `RunStore`。一个 operation 只在 owner contract 中声明一次；planner 把它
  编译为 typed steps，executor 只按 plan 调用 backend。不得重新引入 target/recipe/node/
  action/binding/adapter/policy 多层转述，也不得为 owner 动态执行 Python 注册模块。
- `Project` 是外部 composition seam。公共执行接口只有 open、plan、preflight 和 run；
  backend 是 EDA/tool seam，只有存在真实变化的实现时才建立。旧接口迁移采用替换并删除，
  不提供 alias、兼容 schema、双写或弃用期。
- 公共 import namespace 是 `sigilicon`，实现采用 `src/sigilicon` layout。
- 需要 owner、catalog 或 artifact inventory 的 domain loader 与 workflow 接收同一个显式
  `Project`；只需路径和直接工具操作的窄接口接收显式 `ProjectContext`。最外层 composition
  入口把显式 project root 解析成 `Project`，内部接口不接受 project root 代替 `Project`；
  只有 CLI 可从 cwd 发现 `sigilicon.toml`。
- `virtuoso_bridge` 只能由 `sigilicon.virtuoso` adapter 直接导入。顶层
  `import sigilicon` 和 CLI help 不得要求 Bridge 或 Virtuoso 可用。
- 测试不得启动真实 EDA；需要 Bridge 的测试使用 fake client 或已有离线 helper。
- Sigilicon 不内置 placement、routing 或 physical closure engine；执行内核保持单轮确定
  DAG executor，阶段只交换严格 typed artifacts，不从 report metric 或任意 dict 恢复状态。
  只有 closed result 可生成 executable
  Materialization Plan；DRC/LVS clean 必须同时具有 checked identity、已执行 backend、
  已解析报告和零退出码。测试用 offline/fake Adapter 只能产生非结论状态。
- 完成修改至少运行 package tests、wheel build、clean-wheel import/CLI smoke 与
  `git diff --check`（目录属于 Git 仓库时）。不执行真实 OA 写入或仿真。
