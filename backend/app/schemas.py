"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    # 幂等键的动作序号与拒绝原因：动作被拦下时也回传，页面据此停在原状态并展示原因。
    action_seq: int | None = None
    reject_reason: str | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None



class OdsEntry(BaseModel):
    """接入流水明细结构。"""

    field_0: str | None = None  # 流水编号
    field_1: str | None = None  # 来源系统
    field_2: str | None = None  # 源表名称
    field_3: str | None = None  # 落地表名
    field_4: str | None = None  # 接入周期
    field_5: str | None = None  # 分区粒度
    field_6: str | None = None  # 保留天数
    field_7: str | None = None  # 最近接入时间

class DwdEntry(BaseModel):
    """清洗流水明细结构。"""

    field_0: str | None = None  # 流水编号
    field_1: str | None = None  # 输入表名
    field_2: str | None = None  # 输出表名
    field_3: str | None = None  # 空值填充
    field_4: str | None = None  # 格式转换
    field_5: str | None = None  # 脏数据比例
    field_6: str | None = None  # 执行批次
    field_7: str | None = None  # 清洗状态

class DwsEntry(BaseModel):
    """聚合流水明细结构。"""

    field_0: str | None = None  # 流水编号
    field_1: str | None = None  # 输入表名
    field_2: str | None = None  # 输出表名
    field_3: str | None = None  # 聚合字段
    field_4: str | None = None  # 汇总粒度
    field_5: str | None = None  # 时间窗口
    field_6: str | None = None  # 预计算开关
    field_7: str | None = None  # 执行批次

class AdsEntry(BaseModel):
    """输出流水明细结构。"""

    field_0: str | None = None  # 流水编号
    field_1: str | None = None  # 输入表名
    field_2: str | None = None  # 输出表名
    field_3: str | None = None  # 输出格式
    field_4: str | None = None  # 推送目标
    field_5: str | None = None  # 推送周期
    field_6: str | None = None  # 缓存时间
    field_7: str | None = None  # 执行批次

class DimdefEntry(BaseModel):
    """维度定义明细结构。"""

    field_0: str | None = None  # 维度编号
    field_1: str | None = None  # 维度名称
    field_2: str | None = None  # 源表名称
    field_3: str | None = None  # 自然键列
    field_4: str | None = None  # 代理键列
    field_5: str | None = None  # 缓慢变化策略
    field_6: str | None = None  # 生效日期
    field_7: str | None = None  # 失效日期

class FactdefEntry(BaseModel):
    """事实定义明细结构。"""

    field_0: str | None = None  # 事实编号
    field_1: str | None = None  # 事实名称
    field_2: str | None = None  # 源表名称
    field_3: str | None = None  # 粒度说明
    field_4: str | None = None  # 关联维度
    field_5: str | None = None  # 度量字段
    field_6: str | None = None  # 分区字段
    field_7: str | None = None  # 最近刷新

class PartitionEntry(BaseModel):
    """分区配置明细结构。"""

    field_0: str | None = None  # 配置编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 分区字段
    field_3: str | None = None  # 分区类型
    field_4: str | None = None  # 保留天数
    field_5: str | None = None  # 预建天数
    field_6: str | None = None  # 冷热分离
    field_7: str | None = None  # 存储路径

class QualityEntry(BaseModel):
    """质量规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 校验字段
    field_3: str | None = None  # 校验类型
    field_4: str | None = None  # 阈值上界
    field_5: str | None = None  # 阈值下界
    field_6: str | None = None  # 阻断级别
    field_7: str | None = None  # 最近校验

class LineageEntry(BaseModel):
    """血缘节点明细结构。"""

    field_0: str | None = None  # 节点编号
    field_1: str | None = None  # 节点名称
    field_2: str | None = None  # 节点类型
    field_3: str | None = None  # 上游节点
    field_4: str | None = None  # 下游节点
    field_5: str | None = None  # 字段映射
    field_6: str | None = None  # 转换逻辑
    field_7: str | None = None  # 图谱版本

class BackfillEntry(BaseModel):
    """重跑批次明细结构。"""

    field_0: str | None = None  # 批次编号
    field_1: str | None = None  # 目标表名
    field_2: str | None = None  # 开始日期
    field_3: str | None = None  # 结束日期
    field_4: str | None = None  # 并行数量
    field_5: str | None = None  # 覆盖模式
    field_6: str | None = None  # 重跑状态
    field_7: str | None = None  # 异常区间

class MetadataEntry(BaseModel):
    """元数据记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 字段名称
    field_3: str | None = None  # 字段类型
    field_4: str | None = None  # 业务含义
    field_5: str | None = None  # 统计信息
    field_6: str | None = None  # 更新频率
    field_7: str | None = None  # 负责人

class EtlEntry(BaseModel):
    """调度任务明细结构。"""

    field_0: str | None = None  # 任务编号
    field_1: str | None = None  # 任务名称
    field_2: str | None = None  # 依赖任务
    field_3: str | None = None  # 调度表达式
    field_4: str | None = None  # 重试次数
    field_5: str | None = None  # 超时分钟
    field_6: str | None = None  # 告警渠道
    field_7: str | None = None  # 任务状态

class LakeEntry(BaseModel):
    """湖存储节点明细结构。"""

    field_0: str | None = None  # 节点编号
    field_1: str | None = None  # 存储路径
    field_2: str | None = None  # 文件格式
    field_3: str | None = None  # 压缩方式
    field_4: str | None = None  # 总容量
    field_5: str | None = None  # 已用容量
    field_6: str | None = None  # 访问策略
    field_7: str | None = None  # 节点状态

class CatalogEntry(BaseModel):
    """演进记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 变更类型
    field_3: str | None = None  # 变更字段
    field_4: str | None = None  # 变更前值
    field_5: str | None = None  # 变更后值
    field_6: str | None = None  # 变更批次
    field_7: str | None = None  # 变更时间

class MonitorEntry(BaseModel):
    """监控指标明细结构。"""

    field_0: str | None = None  # 指标编号
    field_1: str | None = None  # 指标名称
    field_2: str | None = None  # 监控对象
    field_3: str | None = None  # 指标类型
    field_4: str | None = None  # 告警阈值
    field_5: str | None = None  # 采样周期
    field_6: str | None = None  # 最近数值
    field_7: str | None = None  # 监控状态

class SnapshotEntry(BaseModel):
    """快照记录明细结构。"""

    field_0: str | None = None  # 快照编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 快照时间
    field_3: str | None = None  # 快照大小
    field_4: str | None = None  # 保留天数
    field_5: str | None = None  # 恢复校验
    field_6: str | None = None  # 快照状态
    field_7: str | None = None  # 创建批次
