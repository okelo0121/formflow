const { sendJson } = require('./_lib');

function draft(prompt) {
  const title = String(prompt || 'Untitled form').replace(/^(please\s+)?(create|make|generate|build)\s+(me\s+)?(a|an)\s+/i, '').slice(0, 70);
  return {
    title: title.charAt(0).toUpperCase() + title.slice(1),
    description: 'Please take a few minutes to complete this form.',
    theme: '#673ab7',
    collectEmail: /email|job|rsvp/i.test(prompt || ''),
    questions: [
      { type: 'short', title: 'Your name', required: false, options: [] },
      { type: 'multiple_choice', title: 'Overall, how would you rate this?', required: true, options: ['Excellent', 'Good', 'Okay', 'Poor'] },
      { type: 'linear_scale', title: 'How likely are you to recommend this?', required: true, options: [], scaleMin: 1, scaleMax: 10, scaleMinLabel: 'Not likely', scaleMaxLabel: 'Extremely likely' },
      { type: 'paragraph', title: 'Anything else we should know?', required: false, options: [] }
    ]
  };
}

module.exports = async (req, res) => {
  if (req.method !== 'POST') { sendJson(res, 405, { ok: false, error: 'POST only' }); return; }
  let body = req.body;
  if (!body || typeof body === 'string') {
    try {
      if (typeof body === 'string') body = JSON.parse(body);
      else {
        const chunks = [];
        for await (const c of req) chunks.push(c);
        body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
      }
    } catch (e) { body = {}; }
  }
  const prompt = String(body.prompt || '').trim();
  if (prompt.length < 3) { sendJson(res, 200, { ok: false, error: 'Describe the form you want first.' }); return; }

  try {
    const ctrl = AbortSignal.timeout(12000);
    const r = await fetch('https://text.pollinations.ai/openai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'openai',
        messages: [
          { role: 'system', content: 'Return ONLY JSON {title,description,theme,collectEmail,questions:[{type,title,required,options,scaleMin,scaleMax,scaleMinLabel,scaleMaxLabel}]}. Types: short,paragraph,multiple_choice,checkbox,dropdown,linear_scale,date,time. 5-8 questions.' },
          { role: 'user', content: prompt }
        ]
      }),
      signal: ctrl
    });
    if (r.ok) {
      const data = await r.json();
      const text = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
      const start = String(text).indexOf('{');
      const end = String(text).lastIndexOf('}');
      if (start >= 0 && end > start) {
        const spec = JSON.parse(text.slice(start, end + 1));
        if (spec.questions && spec.questions.length) {
          sendJson(res, 200, { ok: true, spec, provider: 'pollinations', model: data.model || 'openai', cached: false });
          return;
        }
      }
    }
  } catch (e) {}
  sendJson(res, 200, { ok: true, spec: draft(prompt), provider: 'formflow', model: 'formflow-draft', cached: false });
};
