const { load } = require('./dom-harness');
const H = load().hooks;
H.state.members = new Map([['m1', {id:'m1', name:'alice'}]]);
console.log('unknown ->', JSON.stringify(H.collectMentionMatches('ping @ali please', null)));
console.log('email   ->', JSON.stringify(H.collectMentionMatches('mail me@alice.com', null)));
console.log('composer->', JSON.stringify(H.renderComposerMentionHighlights('<script>x</script> @alice')));
