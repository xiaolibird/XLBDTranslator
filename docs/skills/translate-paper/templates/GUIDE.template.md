# 翻译规范（{ARXIV_ID}，{LICENSE}）

任务：把给定 LaTeX 片段里的英文正文逐句译成简体中文，输出可直接编译（xelatex + ctex）的 LaTeX。

## 必须原样保留
- 所有数学（$...$、\(...\)、\[...\]、equation/align 等环境内部）一字不改，包括其中的 \text{}（编排者统一处理）
- \label{} \ref{} \cref{} \Cref{} \cite*{} \eqref{} 的参数、\includegraphics 路径、环境名、表格列格式、数字
- 方法名、数据集名、代码标识、软件名
- 常用缩写保持英文：{KEEP_ABBR}
- 行与行的对应关系尽量保持（便于对照），注释行（% 开头）原样留着不译
- 花括号必须配平；不要新增宏包或自定义命令
- 作者名、机构、邮箱、关键词元数据保持英文

## 要翻的
正文、标题（\title / \icmltitle 等）、章节标题、\caption、\paragraph/\textbf 小标题、定理类环境方括号里的名字、表格表头里的英文单词（数字不动）、脚注、证明里的文字、算法注释里的自然语言。

## 术语表（全文统一，首次出现写「中文（English）」，之后只用中文或缩写）
{GLOSSARY}

## 交叉引用的文字
Theorem~\ref → 定理~\ref，Lemma → 引理，Corollary → 推论，Proposition → 命题，Assumption → 假设，Remark → 注，Definition → 定义，Appendix → 附录，Sec./Section~\ref{x} → 第~\ref{x}~节，Fig./Figure → 图~\ref，Table → 表~\ref，Eq. → 式~\eqref，Algorithm → 算法~\ref。定理类环境本身的名字由导言区统一处理，不要动 \newtheorem。

## 标点
中文正文用全角标点（，。；：（）“”）。英文引号 ``...'' 和 '...' 改成“……”。数学两侧的中文不用加空格（ctex 会自动处理）。

## 逐字翻译 + 可读性
1. 信息守恒第一：每一句都要对应原文，不删、不增、不概括，限定词（may/often/typically/near/roughly）保留原强度。
2. 在忠实的前提下按「去 AI 味」规则调整句式，规则全文见 /Users/xiaolibird/.claude/skills/lieflat-less-ai-tone/SKILL.md，先读一遍。翻译里最常用到的是第 10 条五种翻译腔（过长前置定语拆分句、“当……时”、“对于……来说”话题壳、句首“然而/因此/此外”路标、“这意味着”复述句）和第 4 条破折号（英文 em dash 改用逗号、括号或拆句）。
3. 原文本身的“not A but B”“rather than”是论证内容，必须译出，不按第 1 条删。证明里推理步骤之间的“因此/此外”可保留。
4. 长英文句可拆成两三个中文短句，但不得改变段落顺序或段落数。
