# AGENTS.md — Sigilicon Python package

适用于本目录及全部子目录。Sigilicon 是可跨项目复用的 EDA execution/evidence kernel；
只拥有 project composition、operation planning、受管执行、artifact/result identity、公共
安全检查，以及真实复用的 tool adapter。具体 IP（含层次化 composite IP）、PDK、target、
qualification 门槛和仓库布局由调用项目拥有。标准 ASIC、模拟/混合信号和 custom layout
保持各自的领域 contract，通过同一个 Project -> ExecutionPlan -> RunResult 生命周期组合，
不得为统一入口而拍平领域 payload。

- 本仓库只维护 `AGENTS.md` 作为 agent 术语、ownership 和工作规则入口；不得新增
  `CONTEXT.md`、`docs/` 或平行架构说明。架构事实只由 source、类型和测试表达。
- 公共执行词汇固定为 `Project`、`Step`、`Adapter`、`ExecutionPlan`、`RunResult` 和
  `RunStore`。一个 operation 只在 owner contract 中声明一次；planner 把它编译为包含完整
  source/resource closure 的 typed steps，executor 只按 plan 调用 adapter。不得为同一事实
  增加多层转述，也不得为 owner 动态执行 Python 注册模块。
- `Project` 是外部 composition seam。公共执行接口只有 open、plan、preflight 和 run；
  adapter 是 package-owned trusted code，不是同进程插件沙箱；owner 不能注入 adapter Python。
  adapter 启动的工具必须使用公共 no-follow/process supervisor seam，不能把裸路径检查冒充
  外部进程隔离。只有存在真实变化的实现时才建立 adapter。旧接口迁移采用替换并删除，
  不提供 alias、兼容 schema、双写或弃用期。
- `sigilicon.toml` 的 `[runtime]` 是调用项目拥有的 deployment contract。tool executable、
  PDK asset、bridge endpoint 和 capability 采用显式 binding，允许绝对路径；Sigilicon 不从
  ambient `PATH` 或全局 site default 推断这些绑定。owner operation catalog 用 runtime
  profile 把 runner 环境名映射到逻辑 resource identity；项目 `[runtime]` 只显式继承列出的
  动态环境名，adapter 不得内置某个 IP 的 library flavor、corner、PDK 文件组织或环境前缀。
- component contract 的 `[sources]` 为每个 owner source 声明唯一 identity，`[filesets]`
  只组合这些 identity；release collateral 直接引用 component/source identity。不得重复路径、
  建立只转发一个文件的 release fileset，或让 fileset 同时承担 source inventory 和发布寻址。
- platform 的 simulation、OA、layout/verification capability 相互正交；loader 只要求至少
  一个 capability，具体 workflow 在自己的 seam 要求所消费的 capability，不得让纯数字
  platform 为满足模拟默认值而声明虚假 contract。
- 公共 import namespace 是 `sigilicon`，实现采用 `src/sigilicon` layout。
- 公共命令只有 `sigilicon {check,flow,oa,release}` 这一棵命令树；子命令实现是不可独立
  执行的内部模块。不得新增并行 console script、`python -m sigilicon.cli.<subcommand>`
  入口或只转发参数的 CLI wrapper。
- 需要 owner、catalog 或 artifact inventory 的 domain loader 与 workflow 接收同一个显式
  `Project`；只需路径和直接工具操作的窄接口接收显式 `ProjectContext`。最外层 composition
  入口把显式 project root 解析成 `Project`，内部接口不接受 project root 代替 `Project`；
  只有 CLI 可从 cwd 发现 `sigilicon.toml`。
- `virtuoso_bridge` 只能由 `sigilicon.virtuoso` adapter 直接导入。顶层
  `import sigilicon` 和 CLI help 不得要求 Bridge 或 Virtuoso 可用。
- package test 不启动真实 EDA；需要 Bridge 的测试使用 fake client 或已有离线 helper。
  调用项目明确授权的端到端验证按该项目 owner 的受管入口执行。
- Sigilicon 不内置 placement、routing 或 physical closure engine；执行内核保持单轮确定
  DAG executor，阶段只交换严格 typed artifacts，不从 report metric 或任意 dict 恢复状态。
  只有 closed result 可生成 executable
  Materialization Plan；DRC/LVS clean 必须同时具有 checked identity、已执行 adapter、
  已解析报告和零退出码。测试用 offline/fake Adapter 只能产生非结论状态。
- 重构提交至少运行 package tests、调用项目的配置/集成测试与 `git diff --check`。wheel
  build 和 clean-wheel smoke 只在发布准备时执行；真实 EDA 只通过调用项目授权的受管入口。
