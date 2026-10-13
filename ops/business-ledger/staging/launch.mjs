import {createServer} from './bff.mjs';
const origin=process.env.OS_STAGE_ORIGIN||'';
const url=new URL(origin);
if(url.protocol!=='https:'||url.origin!==origin||url.port||url.username||url.password||url.hostname==='assafweb.com'||url.hostname.endsWith('.assafweb.com'))throw Error('stage_origin_invalid');
if(process.env.OS_STAGE_BUSINESS_TEST_ENABLED!=='true')throw Error('stage_business_exact_test_opt_in_required');
if(Object.keys(process.env).some(key=>/^(MIA_|AWS_|OPENAI_|GEMINI_|COMPOSIO_)/i.test(key)||key==='GOOGLE_APPLICATION_CREDENTIALS'||key==='NODE_TLS_REJECT_UNAUTHORIZED'))throw Error('stage_ambient_credentials_denied');
const server=await createServer({origin,backend:origin,frontendRoot:'/opt/mobile',telegramBotId:process.env.OS_STAGE_PUBLIC_BOT_ID||'',telegramBotUsername:process.env.OS_STAGE_BOT_USERNAME||'',localTest:false,businessLedgerEnabled:true});
server.on('error',()=>{process.stderr.write('stage_bff_unavailable\n');process.exitCode=1;});
server.listen(4176,'0.0.0.0');
