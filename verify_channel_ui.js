const { chromium } = require('playwright');

const BASE_URL = 'http://127.0.0.1:19015';
const ADMIN_USER = 'admin';
const ADMIN_PASS = 'Ghc@19851210';

let passed = 0, failed = 0;
const results = [];
const fail = (name, detail) => { failed++; results.push(`  ❌ ${name}: ${detail || 'FAILED'}`); };
const ok = (name, detail) => { passed++; results.push(`  ✅ ${name}${detail ? ' (' + detail + ')' : ''}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);

  console.log('→ 1. 登录');
  await page.goto(`${BASE_URL}/auth/login`, { waitUntil: 'networkidle' });
  await page.locator('#username').fill(ADMIN_USER);
  await page.locator('#password').fill(ADMIN_PASS);
  await page.locator('#submitBtn').click();
  try { await page.waitForURL(u => !u.pathname.includes('login'), { timeout: 10000 }); } catch(e) {}
  await sleep(1500);

  console.log('→ 2. 进入渠道对接中心 → 点"开始初始化" → 跳到 "1 企业应用和回调配置"');
  page.setDefaultNavigationTimeout(60000);
  await page.goto(`${BASE_URL}/`, { waitUntil: 'commit', timeout: 60000 });
  await sleep(8000);

  await page.goto(`${BASE_URL}/#/settings/integrations`, { waitUntil: 'commit' });
  await sleep(5000);

  const startBtn = page.locator('button, a').filter({ hasText: /^开始初始化$/ }).first();
  if (await startBtn.count() > 0) {
    console.log('   点"开始初始化"');
    await startBtn.click().catch(()=>{});
    await sleep(4000);
  }

  const step1Card = page.locator('button, a, div').filter({ hasText: /企业应用和回调配置/ }).first();
  if (await step1Card.count() > 0) {
    console.log('   点 "1 企业应用和回调配置"');
    await step1Card.click().catch(()=>{});
    await sleep(4000);
  }

  // 兜底
  const connTab = page.locator('button, [role="tab"], a').filter({ hasText: /^渠道连接$/ }).first();
  if (await connTab.count() > 0) { await connTab.click().catch(()=>{}); await sleep(2000); }
  const fsTile = page.locator('button, a, [role="tab"]').filter({ hasText: /^飞书$/ }).first();
  if (await fsTile.count() > 0) { await fsTile.click().catch(()=>{}); await sleep(2000); }
  const editCreds = page.locator('button').filter({ hasText: /^修改凭证$/ }).first();
  if (await editCreds.count() > 0) { await editCreds.click().catch(()=>{}); await sleep(2500); }

  // Capture screenshot for inspection
  const shotPath = '/tmp/channel_ui_after.png';
  await page.screenshot({ path: shotPath, fullPage: true });
  console.log(`   截图保存: ${shotPath}`);

  console.log('→ 3. 断言 4 个核心改动');

  // Assertion 1: 红框已删除
  const redboxCount = await page.getByText('需要飞书后台像素级指引', { exact: false }).count();
  redboxCount === 0 ? ok('A1 红框已删除', '"需要飞书后台像素级指引" 0 命中') : fail('A1 红框仍存在', `命中 ${redboxCount} 次`);

  // Assertion 2: 字段分组小标题（凭证 / 加密策略 / 选填）
  // 需要在配置表单 region 里查找小标题，避免误命中其他位置文字
  const formScope = page.locator('h3:has-text("企业应用配置"), h3:has-text("修正企业应用凭证")').first();
  const formExists = await formScope.count() > 0;

  // 找到表单卡片的 root
  const cardRoot = formScope.locator('xpath=ancestor::div[contains(@class,"rounded-2xl")][1]');
  const cardCount = await cardRoot.count();
  if (cardCount === 0) {
    fail('A2-prep 找到表单卡片 root', '未找到 rounded-2xl 容器');
  } else {
    const titles = ['凭证', '加密策略', '选填'];
    for (const t of titles) {
      const c = await cardRoot.locator(`div.font-semibold:text-is("${t}"), div:text-is("${t}")[class*="font-semibold"]`).count();
      const fallback = await cardRoot.getByText(t, { exact: true }).count();
      const hit = c + fallback;
      hit > 0 ? ok(`A2 字段分组标题 "${t}"`, `命中 ${hit}`) : fail(`A2 字段分组标题 "${t}"`, '未命中');
    }
  }

  // Assertion 3: 组级"打开飞书对应位置 →"按钮存在 (凭证 + 加密策略 应该各 1 个,选填组无)
  const groupBtnCount = await page.locator('button').filter({ hasText: /打开.+对应位置.*→/ }).count();
  groupBtnCount === 2 ? ok('A3 组级按钮 = 2', `命中 ${groupBtnCount}`) :
    (groupBtnCount > 0 ? fail('A3 组级按钮数错误', `期望 2 实际 ${groupBtnCount}`) :
     fail('A3 组级按钮缺失', `命中 ${groupBtnCount}`));

  // Assertion 4: 字段标签后的"?"按钮已降权 (无 bg-blue-50 / border-blue-200)
  const oldStyleQ = await page.locator('button.bg-blue-50.border-blue-200').filter({ hasText: '?' }).count();
  const newStyleQ = await page.locator('button.text-gray-400').filter({ hasText: '?' }).count();
  oldStyleQ === 0 ? ok('A4a 旧蓝胶囊"?"已消失', '0 命中') : fail('A4a 旧蓝胶囊"?"仍存在', `命中 ${oldStyleQ}`);
  newStyleQ > 0 ? ok('A4b 灰色"?"已就位', `命中 ${newStyleQ}`) : fail('A4b 灰色"?"未渲染', `命中 ${newStyleQ}`);

  // Assertion 5: 检查范围副文案存在 + 旧灰色卡片消失
  const newSubText = await page.getByText(/保存后会检查 L1.*L2.*Scope/).count();
  newSubText > 0 ? ok('A5a 检查范围副文案就位', `命中 ${newSubText}`) : fail('A5a 检查范围副文案缺失', `命中 ${newSubText}`);
  const oldRangeBlock = await page.getByText('保存后检查范围', { exact: true }).count();
  oldRangeBlock === 0 ? ok('A5b 旧"保存后检查范围"灰卡已删', '0 命中') : fail('A5b 旧灰卡仍存在', `命中 ${oldRangeBlock}`);

  // Assertion 6: 右栏 4 助手按钮砍到 2
  // 老按钮文案: 1.复制密钥 / 2.配置回调 / 3.最后检查 / 完整步骤
  const oldHelp1 = await page.getByText('1. 复制密钥').count();
  const oldHelp2 = await page.getByText('2. 配置回调').count();
  const oldHelp3 = await page.getByText('3. 最后检查').count();
  const newHelp1 = await page.getByText('打开像素级指引', { exact: false }).count();
  const newHelp2 = await page.getByText('完整步骤', { exact: false }).count();
  (oldHelp1 + oldHelp2 + oldHelp3 === 0) ? ok('A6a 旧 3 个助手按钮已删', '0 命中') :
    fail('A6a 旧助手按钮残留', `1.复制密钥=${oldHelp1} 2.配置回调=${oldHelp2} 3.最后检查=${oldHelp3}`);
  (newHelp1 > 0 && newHelp2 > 0) ? ok('A6b 新 2 助手按钮就位', `打开像素级指引=${newHelp1} 完整步骤=${newHelp2}`) :
    fail('A6b 新助手按钮缺失', `打开像素级指引=${newHelp1} 完整步骤=${newHelp2}`);

  await browser.close();

  console.log('\n=== 结果 ===');
  results.forEach(r => console.log(r));
  console.log(`\n通过 ${passed}  失败 ${failed}`);
  process.exit(failed === 0 ? 0 : 1);
})().catch(e => { console.error('FATAL:', e); process.exit(2); });
