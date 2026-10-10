/** 中文界面文案。
 *
 *  与 `en.ts` 结构完全对应：这里缺少的 key 会回退到英文，所以两个文件的分组必须
 *  保持一致，不要各自重新组织。
 *
 *  术语保持英文，不硬翻。campaign、run、baseline、canary、benchmark、replay、
 *  redline、policy、worker、tick，以及 TTFT / TPM / GPU / LLM / YAML / API 这类
 *  缩写，都是工程师日常直接说的词——翻成中文只会让人多做一次心算，也搜不到对应的
 *  日志和文档。中英混排在这类工程界面里本来就是自然的写法。
 *
 *  指标名、状态值、引擎参数、benchmark slug、机器名一律不翻译：它们是标识符。
 *
 *  类型写成 `typeof en`：少一个 key、拼错一个 key，`npm run typecheck` 就会失败。
 *  运行时的英文回退只是兜底，不是让翻译可以半途而废的借口。
 */
import type en from './en'

const zh: typeof en = {
  reports: {
    title: '评测报告',
    subtitle: 'Agent 根据已完成的运行写出的性能报告：一个 baseline、为它选定的若干优化尝试，'
      + '以及平台当时算出的对比数据快照。',
    empty: '还没有报告。Agent 通过 agent API 保存（POST /api/agent/v1/reports），'
      + '见 docs/api/agent-api.md。',
    baseline: 'Baseline 运行',
    attempts: '优化尝试',
    campaign: 'Campaign',
    group: '分组',
    by: '保存者',
    when: '保存时间',
    notComparable: '不可比',
    notComparableHint: '平台本身不会把这些运行放在一起比较；报告是在这个前提下保存的。',
    open: '打开',
    back: '全部报告',
    inputs: '来源运行',
    generator: '生成方',
    download: '下载源文件(.zip)',
    downloadHint: 'Markdown、数据和渲染器——在其他地方重新渲染报告所需的全部文件',
    loading: '加载中…',
    missing: '没有这个报告。',
    exportHtml: '导出 HTML',
    exported: '已导出 HTML',
    exportFailed: '导出失败',
    language: '语言',
  },
  nav: {
    campaigns: 'Campaigns',
    searchSpaces: '搜索空间',
    objectives: '优化目标',
    policies: 'Policies',
    tuning: '自动调优',
    baselines: '基线',
    runs: '运行记录',
    resources: '机器资源',
    apiKeys: 'API keys',
    reports: '评测报告',
    logOut: '退出',
    account: '账号',
    signedInAs: '当前登录',
    language: '语言',
  },

  account: {
    title: '账号',
    publish: {
      title: '发布名称',
      nameField: '发布为',
      nameNote: '你的提交在 benchmark 平台上显示的名字，默认是用户名。只是一个显示名称，'
        + '平台不会用它做身份校验。',
      nameSave: '保存名称',
      nameClear: '使用用户名',
      nameSaved: '发布名称已保存',
      nameCleared: '已改回用户名',
      note: '可选。平台代你提交的记录会以这个名字列出。',
      saveFailed: '无法保存发布名称',
    },
    profile: '基本信息',
    role: '角色',
    manageKeys: '管理 API keys',
    changePassword: '修改密码',
    currentPassword: '当前密码',
    newPassword: '新密码',
    confirmPassword: '确认新密码',
    tokenNote:
      '当前会话不会中断。API keys 是另一套凭证，不受影响 —— 需要作废请到 API keys 页面。',
    needCurrent: '请输入当前密码',
    tooShort: '至少 6 个字符',
    mismatch: '两次输入的新密码不一致',
    sameAsOld: '新密码和当前密码相同',
    changed: '密码已修改',
    changeFailed: '修改密码失败',
    users: '用户',
    usersNote: 'admin 可以租借机器、创建 API keys、修改角色',
    promote: '设为 admin',
    demote: '设为 user',
    lastAdmin: '这是唯一的 admin —— 请先提升另一个人',
    roleChanged: '{user} 现在是 {role}',
    roleFailed: '修改角色失败',
  },

  common: {
    cancel: '取消',
    save: '保存',
    close: '关闭',
    copy: '复制',
    download: '下载',
    refresh: '刷新',
    stop: '停止',
    id: 'ID',
    name: '名称',
    status: '状态',
    created: '创建时间',
    started: '开始时间',
    took: '耗时',
    model: '模型',
    machine: '机器',
    config: '配置',
    none: '无',
  },



  login: {
    tagline: 'LLM 推理服务的部署参数自动调优。',
    username: '用户名',
    password: '密码',
    logIn: '登录',
    createAccount: '注册',
    haveAccount: '已有账号？去登录',
    newHere: '还没有账号？去注册',
    failed: '请求失败',
  },

  campaigns: {
    title: 'Campaigns',
    newCampaign: '新建 campaign',
    running: '{n} 个进行中',
    waiting: '，{n} 个等待今晚运行',
    window: '运行窗口',
    search: '搜索方式',
    oneOff: '单次窗口',
    manual: '手动',
  },



  runs: {
    title: '运行记录',
    campaign: 'Campaign',
    failure: '失败原因',
    container: '容器',
    submission: 'LLMBench 提交',
    stopTitle: '停止 run',
    stopConfirm: '停止 run {id}？它的容器会被销毁，机器随即释放。',
    stopIt: '确认停止',
    stopRequested: '已请求停止 —— worker 下一个 tick 生效',
    stopFailed: '停止失败',
    run: 'Run',
    count: '{n} 个 run',
    none: '还没有 run —— 等到有一台对应卡型的机器空闲就会开始。',
    logs: '日志',
    status: '状态',
    gpus: 'GPU',
    endpoint: 'Endpoint',
    started: '开始于',
    took: '耗时',
    stop: '停止',
    launchCommand: '启动命令',
    notLaunched: '（尚未启动）',
    config: '配置',
    results: '结果',
    source: '来源',
    passed: '通过',
    metrics: '指标',
    metricCount: '{n} 个指标',
    noMetrics: '无',
    noResults: '还没有结果。',
    error: '错误',
    capturedLog: '捕获的日志',
    logWhileLive: 'run 仍在运行：目前已捕获的日志。',
    yes: '是',
    no: '否',
  },

  campaign: {
    evaluated: '已评估 {done}/{total} 个候选配置',
    grid: '搜索空间',
    objective: '目标',
    enumerates: '逐个尝试全部配置',
    policy: 'policy',
    report: '报告',
    logs: '日志',
    exportYaml: '导出 YAML',
    more: '更多',
    clone: '克隆',
    cloneName: '副本名称（保留此 campaign 的定义，不含结果）',
    cloneFailed: '克隆失败',
    rerunFailedOne: '重跑 {n} 个失败配置',
    rerunFailedMany: '重跑 {n} 个失败配置',
    pause: '暂停',
    forceStop: '强制停止',
    forceStart: '强制启动',
    resumeSchedule: '恢复定时',
    start: '启动',
    tabs: {
      leaderboard: '排行榜',
      runs: '运行',
      configuration: '配置',
      candidates: '候选配置',
    },
    verifiedOn: '真实流量 replay 结果',
    screening: '全部候选',
    replayedOn: '回放数据集',
    datasetAdopted: '沿用',
    otherDataset: '数据集不同',
    vsBaseline: '对比基线',
    baselineRow: '基线',
    redlines: 'Redlines',
    held: '通过',
    crossed: '越线',
    noRuns: '还没有成功的 benchmark 结果。',
    run: 'Run',
    sweptParameters: '搜索参数',
    notPlanned: '{n} 个配置，首次运行时展开。',
    editSchedule: '修改定时',
    addSchedule: '添加定时',
    exportTitle: 'Campaign YAML',
    exportNote:
      '只导出输入项 —— 状态、当前窗口、force start 的 override 都不包含，'
      + '所以导入得到的是一个新建的 campaign，而不是恢复一个跑到一半的夜晚。',
    yamlCopied: 'YAML 已复制',
    reportCopied: '报告已复制',
    copyMarkdown: '复制 markdown',
    reportTitle: 'Campaign 报告',
  },
  baselines: {
    tuneFromThis: '以此为基础调优',
  },
}


export default zh
