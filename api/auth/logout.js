module.exports = (req, res) => {
  res.writeHead(302, {
    Location: '/',
    'Set-Cookie': 'ff_session=; Path=/; Max-Age=0'
  });
  res.end();
};
