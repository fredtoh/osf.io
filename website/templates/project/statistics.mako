<%inherit file="project/project_base.mako"/>
<%def name="title()">${node['title']} Analytics</%def>

<div class="page-header  visible-xs">
  <h2 class="text-300">Analytics</h2>
</div>

<div id="hiddenFlag" class="scripted">
  <div class="m-b-md p-md osf-box-lt box-round text-center">The use of adblocking software may prevent site analytics from loading properly. You may need to whitelist this domain in your adblocker.</div>
</div>
<script src="/static/js/ads.js"></script>
<script>
if(!window.canRunAds){
    $('#hiddenFlag').show();
}
</script>

<%
    if user['is_contributor']:
        token = user.get('piwik_token', 'anonymous')
    else:
        token = 'anonymous'

    if node.get('piwik_site_id'):
        piwik_url = '{host}index.php?module=Widgetize&action=iframe&moduleToWidgetize=Dashboard&actionToWidgetize=index&idSite={site_id}&period=day&date=today&disableLink=1&token_auth={auth_token}'.format(
            host=piwik_host,
            auth_token=token,
            site_id=node['piwik_site_id'],
        )
%>

% if not piwik_host or not node['piwik_site_id'] or not node['is_public']:
    <div class="row m-lg">
        <div class="col-xs-12 text-center">
            <img src="/static/img/no_analytics.png">
        </div>
    </div>
% else:
    <iframe style="overflow-y:scroll;border:none;" width="100%" height="600" src="${ piwik_url }"></iframe>
% endif
