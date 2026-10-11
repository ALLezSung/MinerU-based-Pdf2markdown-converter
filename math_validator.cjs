// Task-independent KaTeX syntax validation. Inputs/outputs are JSON file paths.
const fs=require('fs');
const [inputPath,outputPath,modulePath]=process.argv.slice(2);
const katex=require(modulePath);
const input=JSON.parse(fs.readFileSync(inputPath,'utf8'));
const errors=[];
for(const item of input){
  try{katex.renderToString(item.tex,{displayMode:item.display,throwOnError:true,strict:'ignore',trust:false});}
  catch(error){errors.push({...item,error:error.message});}
}
fs.writeFileSync(outputPath,JSON.stringify({checked:input.length,errors},null,2),'utf8');
console.log(JSON.stringify({checked:input.length,errors:errors.length}));
process.exitCode=errors.length?1:0;
