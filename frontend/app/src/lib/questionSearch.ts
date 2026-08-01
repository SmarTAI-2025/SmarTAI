export function questionSearchAliases(source: string) {
  const aliases: string[] = [];
  if (/\\int|∫|积分/i.test(source)) aliases.push("积分 积分题 integral integration");
  if (/证明|prove|proof/i.test(source)) aliases.push("证明 证明题 proof");
  if (/编程|代码|algorithm|code|python|java/i.test(source)) aliases.push("编程 编程题 代码 算法 programming");
  if (/计算|calculate|compute/i.test(source)) aliases.push("计算 计算题 calculation");
  if (/概念|定义|concept|definition|what is/i.test(source)) aliases.push("概念 概念题 定义 concept definition");
  return aliases.join(" ");
}
