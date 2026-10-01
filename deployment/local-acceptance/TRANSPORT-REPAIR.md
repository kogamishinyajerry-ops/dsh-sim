# 传输故障闭环（2026-10-02）

本轮仅改外层运行脚本及独立标准库测试。基线 e266423；不改 Worker、MCP、授权、
领域合同、方法批准或工程阈值。shell 源始于 blob c6def51911e06cd249d247258ca7ffa0e33ef3bf。

原审查样本重新运行：4 passed / 2 failed（失败保留）。修复后：

* Docker 列表/进程清单、部分清单后报错、信号、退出查询或清理失败：UNKNOWN，非零，
  保留原始 rc；不能输出 stopped。`restart` 在未知停止状态下不启动。
* 查询成功且无目标才是 already stopped。pgrep=1 是无匹配，>1 是查询故障。
* /proc 读取失败或 EPERM 不等于进程退出；消失竞争可正常处理，Z/X 视为已退出。
* 拒绝负 PID、PID 1 与非数字 PID，防止进程组或容器 init 被误发信号。
* checked() 在失败分支保存真实退出码，不再读取 ! 运算后的 0。
* 所有新进程查询参数通过位置参数传递；现有启动拼接仅允许简单绝对容器路径、
  数字超时和标识符。不支持含空格/引号的容器配置路径；宿主脚本路径可含空格。

测试命令（无需 Docker 或 Python 第三方包）：

```bash
python3 -m unittest discover -s tests/deployment -p test_transport_failure.py -v
```

28 项通过；20 个 launcher 入口场景加实际 inner shell 检查的总数以测试日志为准。
其中 8 个 inner shell 测试运行真实 Bash 查询逻辑，使用本机 /proc、pgrep/kill 函数替身；
不发送真实 TERM、不调用求解器。其他测试使用严格 Docker/curl 传输替身，未知调用报错。

NOT_RUN：真实 Docker、Mac、DSH 模型、OpenFOAM、本仓全量 Python 回归。当前容器
没有 Docker，且 Git HTTPS DNS 不可用；源文件经连接器与已有逐字节审查样本取得。

## 本地回归与部署限制

在独立验收容器应用后运行已有 test-run-local.sh，再注入 Docker 检查后断连场景。
不要在有真实任务的容器运行故障测试。保留本轮源码与上一轮真实结果的 SHA 区别。
当前部署脚本仍限定为一个专用容器中的单实例：保留旧版锚定 pgrep 的残留进程发现，
不是多实例进程所有权隔离或 PID 重用证明；不要与另一实例共用该容器。
并发启停、传输永久挂起、worker 崩溃后接管均未由此修复实现。
