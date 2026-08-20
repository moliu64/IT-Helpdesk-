from src.ticket_parser import parse_ticket


def test_ten_sample_extraction_accuracy():
    samples = [
        ("标题：VPN 无法连接\n描述：客户端提示超时\n渠道：邮件", "VPN 无法连接", "客户端提示超时", "email"),
        ("主题：密码忘记\n问题描述：无法登录域账号\n渠道：电话", "密码忘记", "无法登录域账号", "phone"),
        ("title: Outlook crash\ndescription: startup error\nchannel: chat", "Outlook crash", "startup error", "chat"),
        ("打印机无法打印\n队列一直显示暂停", "打印机无法打印", "队列一直显示暂停", "portal"),
        ("标题：共享盘无权限\n描述：部门目录访问被拒绝\n渠道：门户", "共享盘无权限", "部门目录访问被拒绝", "portal"),
        ("主题：收到钓鱼邮件\n描述：点击了可疑链接\n渠道：微信", "收到钓鱼邮件", "点击了可疑链接", "chat"),
        ("标题：电脑蓝屏\n描述：开机后反复重启\n渠道：来电", "电脑蓝屏", "开机后反复重启", "phone"),
        ("title: Mail delivery failed\ndescription: messages bounce\nchannel: email", "Mail delivery failed", "messages bounce", "email"),
        ("软件安装申请\n需要安装设计软件", "软件安装申请", "需要安装设计软件", "portal"),
        ("标题：无线网络掉线\n描述：会议室 WiFi 不稳定\n渠道：工单系统", "无线网络掉线", "会议室 WiFi 不稳定", "portal"),
    ]
    correct = 0
    for text, title, description, channel in samples:
        ticket = parse_ticket(text)["ticket"]
        correct += sum((ticket["title"] == title, ticket["description"] == description, ticket["channel"] == channel))
    assert correct / 30 >= 0.8
