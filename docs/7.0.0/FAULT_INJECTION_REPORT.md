# 7.0.0 故障注入报告

状态：自动化故障矩阵通过；系统休眠/唤醒仍需人工环境验收。

## 已覆盖故障

- Sidecar 中断、重启、父进程退出与认证关闭。
- 模型临时 429、额度耗尽和 WAITING_PROVIDER 恢复。
- 任务租约过期、Heartbeat、僵尸 RUNNING 回收。
- Managed Process 停止、任务取消和孤儿进程清理。
- 崩溃后文件副作用去重与检查点恢复。
- 持久队列重启、取消、提升顺序和事件恢复。

## 自动化结果

```text
pytest test_recovery.py test_provider.py test_process_supervisor.py test_task_leases.py test_task_runtime.py test_stability.py
34 passed in 20.91s
```

相关完整后端回归：347 passed，1 skipped。

## 剩余人工项

- Windows 真实休眠/唤醒。
- 运行中改变工作区 ACL。
- 断开桌面 WebView 后长时间保持 Sidecar，再连接恢复。
