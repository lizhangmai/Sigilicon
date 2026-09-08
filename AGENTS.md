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
- `Project` 是外部 composition seam，执行生命周期只有 open、plan、preflight 和 run；
  owner、catalog、resource 和 configuration 查询仍从同一个 `Project` 提供给 domain loader
  与 adapter planner，
  不建立平行 composition 入口。adapter 是 package-owned trusted code，不是同进程插件沙箱；
  owner 不能注入 adapter Python。
  工具插件借鉴 Hammer 的配置、执行与结果收集分工：设计/工艺 Tcl 是调用项目拥有的
  source 输入，adapter 直接执行并绑定参数、资源和 typed artifacts，不生成设计流程 Tcl
  来限制 caller 的定制能力。通用工具报告解析属于 adapter，数值验收属于 owner contract。
  adapter 启动的工具必须使用公共 no-follow/process supervisor seam，不能把裸路径检查冒充
  外部进程隔离。只有存在真实变化的实现时才建立 adapter。旧接口迁移采用替换并删除，
  不提供 alias、兼容 schema、双写或弃用期。
- `sigilicon.toml` 的 `[runtime]` 是调用项目拥有的 deployment contract。tool executable、
  PDK asset、bridge endpoint 和 capability 采用显式 binding，允许绝对路径；Sigilicon 不从
  ambient `PATH` 或全局 site default 推断这些绑定。immutable 输入与 mutable publication
  destination 分别声明；发布目的地的历史内容不进入输入闭包。owner operation catalog 用 runtime
  profile 把 runner 环境名映射到逻辑 resource identity；项目 `[runtime]` 只显式继承列出的
  动态环境名，adapter 不得内置某个 IP 的 library flavor、corner、PDK 文件组织或环境前缀。
- component contract 的 `[sources]` 为每个 owner source 声明唯一 identity，`[filesets]`
  只组合这些 identity；release collateral 直接引用 component/source identity。不得重复路径、
  建立只转发一个文件的 release fileset，或让 fileset 同时承担 source inventory 和发布寻址。
- release view 以 export 内的独立 name 寻址；role 表达用途，variant 与 condition 表达适用
  条件。消费按 name 或明确 selector 唯一解析；publication 与 package audit 使用相同的
  receipt、内容 identity 和 maturity 判据。
- platform 的 simulation、OA、layout、verification capability 相互正交；loader 只要求至少
  一个 capability，具体 adapter operation 在自己的 seam 要求所消费的 capability，不得让纯数字
  platform 为满足模拟默认值而声明虚假 contract。
- 公共 import namespace 是 `sigilicon`，实现采用 `src/sigilicon` layout。
- 公共命令只有 `sigilicon {check,flow,oa,release}` 这一棵命令树；子命令实现是不可独立
  执行的内部模块。不得新增并行 console script、`python -m sigilicon.cli.<subcommand>`
  入口或只转发参数的 CLI wrapper。
- 需要 owner、catalog 或 artifact inventory 的 domain loader 与 adapter planner 接收同一个
  显式 `Project`，但返回的 domain snapshot 只保留 immutable repository identity，不得反向
  持有 composition 对象；只需路径和直接工具操作的窄接口接收显式 context。最外层 composition
  入口把显式 project root 解析成 `Project`，内部接口不接受 project root 代替 `Project`；
  只有 CLI 可从 cwd 发现 `sigilicon.toml`。
- `virtuoso_bridge` 只能由 `sigilicon.virtuoso` adapter 直接导入。顶层
  `import sigilicon` 和 CLI help 不得要求 Bridge 或 Virtuoso 可用。

## 测试设计与验证

- 测试先选择稳定 seam：执行行为从 `Project -> ExecutionPlan -> RunResult/RunStore`、公共
  CLI 或持久化 artifact 进入；domain loader/adapter planner 接收显式 `Project` 并检查稳定返回值
  或 typed result；package-owned adapter 通过 `Adapter`/`ExecutionIO` 行为测试。若一项普通
  行为只能通过私有状态验证，先加深 owning module，让现有 interface 返回足够结论；不为
  测试增加裸字段、debug getter 或平行 public API。
- 回归断言稳定含义：status、identity、错误语义、artifact 内容、受支持的 typed 字段，以及
  canonical persisted contract 中由本模块拥有的字段。`record` 只有在确实作为 CLI、manifest
  或持久化协议被消费时才是测试面，优先验证 writer -> reader/auditor round trip；planner
  action、cache、snapshot 容器和完整中间 dict 不是回归输出。
- 测试表达当前支持的 contract，不保存已删除 API 的墓碑。测试不通过精确 `__all__`、函数
  签名、`__dict__`、dataclass 字段表、属性不存在性或生产源码字符串来规定实现形状；
  `tests/test_suite_policy.py` 持续检查这条边界。
- fixture 使用真实 TOML、owner/catalog 关系和公共 composition 路径；fake Adapter 只从测试
  assembly 接入。mock/monkeypatch 放在 EDA、Bridge、进程、时钟或明确的故障注入边界，不用
  私有 parser/loader 调用次数、对象同一性或 cache 命中证明正确性。性能回归用输入规模与
  可观察资源上界表达。
- no-follow、TOCTOU、进程清理和 OA completion uncertainty 等竞争条件可以在无法从外部稳定
  触发时 patch 最窄的私有 transition point；测试仍须从公共 operation 进入，并断言公共错误、
  terminal result 或 artifact。该例外保持局部，不扩展成通用测试接口。
- package test 不启动真实 EDA；需要 Bridge 的测试使用 fake client 或已有离线 helper。
  调用项目明确授权的端到端验证按该项目 owner 的受管入口执行。
- Sigilicon 不内置 placement、routing 或 physical closure engine；执行内核保持单轮确定
  DAG executor，阶段只交换严格 typed artifacts，不从 report metric 或任意 dict 恢复状态。
  只有 closed result 可生成 executable
  Materialization Plan；DRC/LVS clean 必须同时具有 checked identity、已执行 adapter、
  已解析报告和零退出码。测试用 offline/fake Adapter 只能产生非结论状态。
- 重构提交至少运行 package tests、调用项目的配置/集成测试与 `git diff --check`。wheel
  build 和 clean-wheel smoke 只在发布准备时执行；真实 EDA 只通过调用项目授权的受管入口。
