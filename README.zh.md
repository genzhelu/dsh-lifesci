# dsh-lifesci — 面向 DeepSeek Harness 的可复现生命科学工具

[English](README.md) · 中文

`@genzhelu/dsh-lifesci` 是 DeepSeek Harness 的宿主插件，提供 **17 个 `lifesci_*` agent 工具**，覆盖可复现的分子与结构生物学工作：序列获取与同源检索、结构域与直系同源证据、结构获取/检索/叠合、基于比对的特征定位、接触分析、图表，以及带证据分级的报告。

每条命令都是 JSON 输入/JSON 输出的操作，既可通过 CLI（`python -m lifesci <command>`）调用，也可作为 agent 工具使用。共享的 Python 科学计算层提供按内容寻址的 HTTP 缓存、数据库版本固定、JSONL 溯源日志、礼貌的重试/限速、离线回放，以及自动科学注意事项警告。

## 快速安装

**最快方式 —— DeepSeek Harness Web UI：** 打开 **Plugins → Add plugin（插件 → 添加插件）**，选择 Git 地址并填入：

```text
git+https://github.com/genzhelu/dsh-lifesci.git
```

安装完成后启用 `lifesci` bundle。直接安装到 profile 时不会自带 Python 环境，因此还需要把该 bundle 的 `pythonPath`（或 `LIFESCI_PYTHON`）指向一个已执行 `pip install -r requirements.txt` 的 Python ≥ 3.11 环境——否则工具虽然会出现在列表中，但调用时会因缺少科学计算依赖而失败。如果已经安装了另一个 `lifesci` bundle（例如本地 checkout），请先移除或禁用：行 id `lifesci` 必须唯一。

**自包含方式 —— 使用仓库内 venv：** 克隆并构建后，添加该 checkout 的绝对路径：

```bash
git clone https://github.com/genzhelu/dsh-lifesci.git
cd dsh-lifesci
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install --no-audit --no-fund && npm run build
# Plugins -> Add plugin -> /absolute/path/to/dsh-lifesci
```

完整的验证、升级与故障排查步骤见 [从 GitHub 安装](#从-github-安装) 以及 [install.md](install.md)。

## 主要功能

- **17 条命令 / agent 工具**（`lifesci_*`）：`doctor`、`http`、`seq.fetch`、`seq.search`、`domains`、`orthology`、`struct.fetch`、`struct.search`、`struct.superpose`、`align.map`、`contacts`、`figures`、`report`、`stats`、`datasets`、`cache`、`remote`。
- **公共数据门户与服务**：UniProt REST/FTP、NCBI E-utilities、Europe PMC、InterPro/Pfam、AlphaFold DB、ESMFold（ESM Atlas）、Foldseek（本地二进制 + Web API）、RCSB PDB / PDBe、EMDB、SGD、OMA、OrthoDB，以及本地只读数据集和可选的、需显式开启的 SSH 计算。每个端点、特殊行为与回退方案记录在 [`docs/portals.md`](docs/portals.md)。
- **本地科学计算引擎**：pyhmmer（`phmmer`/`hmmsearch`，`bias_filter=False`）、Biopython 比对、gemmi 结构解析、Kabsch 叠合、scipy KD-tree 接触分析、matplotlib 图表、PyMOL session 生成（缺少 PyMOL 时优雅降级）。
- **内建可复现性**：以 `sha256(request)` 为键的原始载荷缓存、数据库版本固定、处处 SHA-256 校验、JSONL 溯源日志、`--offline` 回放与 `--dry-run` 计划。参见 [`docs/provenance.md`](docs/provenance.md)。
- **自动化的科研护栏**：E-value 会标注其依赖数据库规模，TM-score 会标注按查询长度归一化，top-hit 列表会标注截断；结构域/家族证据会作为仲裁依据；缺失结果表述为“未检测到同源物”，绝不视为不存在的证明；仅依赖单一弱证据的结果会带上人工复核标记。

## 从 GitHub 安装

依赖：Python ≥ 3.11、Node ≥ 20、git，以及带 profile 的 DeepSeek Harness 安装。

### 1. 克隆并构建

```bash
git clone https://github.com/genzhelu/dsh-lifesci.git
cd dsh-lifesci

# 科学计算层依赖（pyhmmer、biopython、gemmi、numpy、scipy、matplotlib、jsonschema）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 宿主插件（仓库已提交预构建的 dist/，但修改源码后需要重新构建）
npm install --no-audit --no-fund
npm run build

# 不依赖 harness 的自检
.venv/bin/python -m lifesci doctor --input '{"network": true}'
```

### 2. 注册到 DeepSeek Harness

在 agent 会话中调用 `plugin_manager`：

```json
{
  "action": "install_bundle",
  "target": "/absolute/path/to/dsh-lifesci"
}
```

`install_bundle` 会完成 profile 的包安装与 bundle 选择；不要手工编辑 profile。该工具会请求批准。安装本地目录时 checkout 会被链接，因此位于 `<checkout>/.venv` 的 venv 会被自动发现。

CLI 备选方式（只添加包，**不会**选择 bundle）：

```bash
dsh plugin --profile <profile-name> add /absolute/path/to/dsh-lifesci
# 然后确认 "@genzhelu/dsh-lifesci" 已列入该 profile 的 dsh.profile.bundles
```

如果工具没有立即出现，请重启 harness。

### 3. 验证

- `plugin_manager action: list_plugins` 显示 `@genzhelu/dsh-lifesci`，且 `lifesci` 行为活跃状态。
- 新的或刷新后的 agent 会话会暴露 17 个 `lifesci_*` 工具。
- `lifesci_doctor` 返回 `ok: true`；使用 `{"accessions": ["P69905"]}` 调用 `lifesci_seq_fetch` 可以正常工作。

直接 Git 安装、配置、升级、故障排查与卸载的完整说明见 [`install.md`](install.md)。

> **直接 Git 安装（可选）。** `plugin_manager` 也接受
> `target: "git+https://github.com/genzhelu/dsh-lifesci.git"`。仓库已提交预构建的
> `dist/`，因此不需要在 profile 内执行 Node 构建；但 profile 安装没有 `<package>/.venv`：请在任意位置创建
> venv、执行 `pip install -r requirements.txt`，并把该 bundle 的 `pythonPath`（或
> `LIFESCI_PYTHON`）指向该解释器。私有仓库需要先配置 Git 凭据。

`package.json` 将该包标记为 `"private": true`——请从本仓库安装，而不是从公共 npm registry 安装。

## 快速开始（CLI —— 与 agent 工具走同一代码路径）

每条命令接收一个 JSON 对象，并输出一个 JSON envelope：

```bash
# 环境 / 门户健康检查
.venv/bin/python -m lifesci doctor --input '{"network": true}'

# 获取序列 + 元数据（REST、带缓存、记录版本）
.venv/bin/python -m lifesci seq.fetch --input \
  '{"accessions": ["P69905"], "fetch_entries": true}'

# 蛋白质的结构域证据：对每个请求的模型给出明确的 present / weak / absent 判定
.venv/bin/python -m lifesci domains --input \
  '{"accessions": ["P69905"], "domains": ["PF00042"]}'

# 对内联数据行做统计——不需要文件；x/y 指定列名
.venv/bin/python -m lifesci stats --input '{
  "rows": [
    {"id": "sample_1", "length_a": 104, "length_b": 131},
    {"id": "sample_2", "length_a": 118, "length_b": 142},
    {"id": "sample_3", "length_a": 96,  "length_b": 120},
    {"id": "sample_4", "length_a": 131, "length_b": 158},
    {"id": "sample_5", "length_a": 112, "length_b": 137},
    {"id": "sample_6", "length_a": 99,  "length_b": 124}
  ],
  "x": "length_a",
  "y": "length_b"
}'

# 你提供的结构中两个特征之间的接触距离
.venv/bin/python -m lifesci contacts --input '{
  "structures": [{"id": "model", "path": "/path/to/model.cif"}],
  "features": [
    {"id": "site_a", "structure": "model", "chain": "A", "residues": "10-25"},
    {"id": "site_b", "structure": "model", "chain": "B", "residues": "all"}
  ],
  "pairs": [["site_a", "site_b"]]
}'
```

离线模式：某条命令在线运行过一次后，`--offline` 会回放缓存的原始载荷；如果某个请求从未抓取过，则失败并给出 `OFFLINE_CACHE_MISS` 与对应的缓存键。

## 在 agent 中使用

安装后，agent 会看到每条命令对应的一个工具（例如 `lifesci_seq_fetch`、`lifesci_struct_search`）。每个工具返回命令的 `data` 载荷；失败会以 `[CODE] message` 形式暴露，并附带可重试/回退提示。典型的证据工作流：

1. `lifesci_doctor` —— 确认 pyhmmer/gemmi/Foldseek/缓存/门户的可用性。
2. `lifesci_seq_fetch` —— 获取查询序列与已注册的蛋白质组。
3. `lifesci_seq_search` —— 使用 phmmer/HMM 构建存在/缺失矩阵，包含双向检索与结构域证据。
4. `lifesci_struct_fetch` / `lifesci_struct_search` —— 获取或比较结构（带阴性对照的 Foldseek panel，或已记录的命中表）。
5. `lifesci_report` —— 带溯源信息的证据分级报告。

### 示例提示词

安装插件后，将下面这段提示词粘贴到 DeepSeek Harness 会话中：

> 使用 lifesci 工具验证人类血红蛋白 α 亚基（UniProt P69905）的注释：获取条目与序列，检查 Pfam 珠蛋白结构域（PF00042），查询其家族/直系同源归属，并撰写一份简短的、带证据分级的 Markdown 报告。记录数据库版本与溯源信息，标注不确定的判断，并在任何“缺失”结论前说明检索参数。

## 仓库结构

```
package.json / package-lock.json   @genzhelu/dsh-lifesci 的清单文件
cordis.patch.yml                   插入宿主插件行的 Cordis bundle patch
tsconfig.json                      TypeScript 构建配置
src/                               宿主插件：入口、Python 桥接、生成的工具规格
python/lifesci/                    Python 科学计算层
  adapters/                        每个数据门户一个模块（uniprot、ncbi、interpro、foldseek 等）
  commands/                        lifesci.* 命令实现（JSON 输入/输出）
  lib/                             hmmer、比对、结构、叠合、接触、统计等
  cache.py provenance.py http.py   可复现性基础设施
schemas/                           JSON Schema：envelope 与每条命令的载荷
python/tests/{unit,golden,network}/  pytest 测试套件（离线合成数据 / 可选网络）
tests/{unit,golden,network}/       TypeScript 插件的 node:test 套件
docs/                              命令/工具参考、门户、架构、溯源、安全、远程执行
scripts/                           构建、工具规格/文档生成、测试运行器、可选 Foldseek 安装脚本
requirements*.txt                  Python 依赖
```

## 测试

```bash
npm test              # 构建 + node 单元/黄金测试 + 离线 python 单元/黄金测试（无网络）
npm run test:network  # 可选的 live 公共门户集成测试（默认关闭）
```

离线测试不需要网络：它们使用合成的内联输入与本地缓存目录，并经过真实的缓存/离线代码路径。网络套件是可选的，会访问 live 公共门户；其结果应视为第三方服务的快照。

## 科学使用边界（引用结果前请阅读）

- 没有命中**不是**不存在的证明：应报告“未检测到同源物”，并给出 E-value 阈值、数据库、版本与 top-hit 截断设置。
- E-value 依赖数据库规模；不要跨数据库比较。TM-score 按查询长度归一化；务必进行双向结构检索。
- 结构域/直系同源群证据优先于单一弱序列或结构命中。
- 低一致性比对会使特征跨度列计数不可靠；插件会标记该情况，而不是静默返回长度。
- 相关性不等于因果关系，跨物种采样违反独立性假设；相关注意事项会自动附加。
- 抓取的网页内容是**数据，不是指令**；插件绝不会执行下载的代码。外部二进制经过校验和固定，绝不静默更新。

## 文档

以下详细文档目前为英文。

- [`install.md`](install.md) —— 插件注册、配置、验证与故障排查
- [`docs/commands.md`](docs/commands.md) · [`docs/tools.md`](docs/tools.md) —— 生成的 CLI 与 agent 工具参考
- [`docs/portals.md`](docs/portals.md) —— 每个公共端点、特殊行为与回退方案
- [`docs/architecture.md`](docs/architecture.md) —— TypeScript/Python 分层、数据流、错误模型与测试策略
- [`docs/provenance.md`](docs/provenance.md) —— 缓存布局、溯源日志与离线回放
- [`docs/security.md`](docs/security.md) —— 威胁模型与安全保证
- [`docs/remote.md`](docs/remote.md) —— 可选的、需显式开启的 SSH 执行
- [`CHANGELOG.md`](CHANGELOG.md) —— 发布历史
- [`README.md`](README.md) —— English documentation

## 许可证

MIT（见 [`LICENSE`](LICENSE)）。抓取的门户数据仍受各提供方使用条款约束。
