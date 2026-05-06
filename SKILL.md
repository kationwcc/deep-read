---
name: deep-read
description: 读取网页内容，如果需要鉴权的场景会弹出鉴权窗口
trigger: /deep-read
when:
  file_pattern: "*.py"
  directory: "src/"
---

# 读取http地址
1. 如果是完整的http协议地址，通过调用scripts/analyze.py进行读取。
2. 在执行analyze.py脚本之前，你需要先验证Playwright库是否有正确安装，如若没有，那你需要执行python的install命令进行安装。
3. 然后通过这个python脚本输出的html代码，作为你的知识库。
4. 最后解答我的实际问题。