# 联动海域许可与海缆施工窗口基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配和调度情景；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `src/sea_permit/`：海域许可联动、空间边界版本、限制时段、责任主体授权、承诺到期、方案评估与撤回处置；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
PYTHONPATH=src python3 -m sea_permit.acceptance --workspace .
```

四条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析、并网审批和海域许可联动评估，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m sea_permit.api --database sea.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 海域许可联动规则

- 许可登记以空间边界版本为核心，每个版本关联限制时段（如渔业协商禁作期）、责任主体授权和承诺到期时间；施工与送出方案必须引用已登记的确定版本。
- 方案评估逐段生成候选：线段须完整落在海域使用边界内、不触碰航道安全区与生态边界、落在运维边界内、与既有电缆保持登记的保护距离，且施工窗口不与限制时段相交、被有效授权覆盖、不晚于承诺到期时间；被排除的线段返回具体依据（约束类别、来源编号和说明）。
- 约束区与既有电缆通过批量导入登记：导入全有或全无，批次号幂等，重复提交返回稳定结果，内容不同则冲突。
- 许可撤回立即阻止尚未确认的草稿方案；已确认和执行中的方案转入人工处置，只能由监管角色显式办结（停止或显式迁移到可用许可版本），系统不做静默迁移；已完成的合法施工记录永远保留。
- 建设、运维、审计角色分别使用 construction、om、audit 三种方案视图，均包含线段被排除的具体依据；审计视图另含评估输入摘要与方案事件流。
