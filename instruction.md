# 换热台重叠阶跃后的回路复原
换热台改造后只剩一条执行器总指令和四路过程波形。总指令是四台执行器稀疏阶跃信号之和，接线表没有留下；每路过渡过程又与相邻事件叠在一起。值班组需要在交接前恢复事件归属、接线和动态参数。
交付程序放在 `/app/loop_recovery.py`；调度命令为 `python3 /app/loop_recovery.py SESSION_JSON RESULT_JSON`。成功时标准输出只有 `RECOVERED <session_id> 4` 一行。本批成功回执为 `RECOVERED BENCH-2026-10-23-HX04 4`。输入不合约时标准输出为空，标准错误只有 `REFUSED INPUT_SCHEMA` 一行。
恢复失败时沿用同一输出通道：配对或事件归属不唯一写 `REFUSED EVENT_SUPPORT`，动态模型超出公开误差上限写 `REFUSED FIT_LIMIT`。除这三种拒绝码外不承诺其它错误分类。
会话为 UTF-8 JSON，键集合如下：

```text
root: {actuators, bench_id, command_bus, loops, sample_count,
       sample_period_s, schema, session_id, thresholds}
actuators[]: {actuator_id, class, delay_range_s, gain_range, tau_range_s}
loops[]: {loop_id, samples}
thresholds: {event_jump_min, event_support_min, fit_rmse_max, mapping_gap_min}
```

`schema` 是 `overlap-recovery/v1`。`actuators` 与 `loops` 都恰好 4 项，标识不重复。`sample_period_s` 为正数；`command_bus` 与每路 `samples` 的长度都等于 `sample_count`，且内容为有限数值。各层出现额外键、缺键或重复 JSON 键时关闭为 `INPUT_SCHEMA`。

每台执行器从零开始，以 3 个未知启动时刻和阶跃幅值形成自身指令。总指令等于四条自身指令在同一采样周期上的和，单台指令轨迹不随会话提供。跳变绝对值达到 `event_jump_min` 即计入事件。

每路过程只受真实执行器驱动，并服从 `G(s) = gain * exp(-delay_s*s) / (tau_s*s + 1)`。恢复的动态参数须落在对应执行器的公开范围内。`fit_rmse_max` 是每路均方根误差的上限。默认批次还要求增益相对误差不超过 2%，时间常数相对误差不超过 6%，纯时延绝对误差不超过 0.04 秒。

回路到执行器是一一对应。每路必须恰好恢复 3 个事件，全批共 12 个；四组事件不能重复使用。全局配对按拟合残差与参数范围形成支持度，支持最高者胜出。最佳与次佳配对的未舍入总支持差低于 `mapping_gap_min` 时关闭为 `EVENT_SUPPORT`。

若两个完整配对的未舍入总支持完全相同，按结果数组顺序对 `actuator_id` 取规范字典序的首项。恢复后任一事件换到次优回路都不能提高相对支持度；最小相对支持差低于 `event_support_min` 时同样关闭为 `EVENT_SUPPORT`。

结果对象的键集合如下：

```text
result: {analysis, bench_id, loops, schema_version, session_id}
analysis: {event_count, event_support_margin, global_rmse, mapping_margin}
loops[]: {actuator_id, delay_s, event_count, fit_rmse, gain, loop_id, tau_s}
```

`analysis.global_rmse` 取各路 `fit_rmse` 的最大值，`event_count` 为全批事件数。数值按半进位舍入到六位小数；JSON 数字不补尾随零。`loops` 按 `loop_id` 升序，结果文件采用 UTF-8、键名升序、紧凑 JSON，末尾一个换行。

四路、320 点的完整批次须在 6 秒内完成，复核 watchdog 为 8 秒。
