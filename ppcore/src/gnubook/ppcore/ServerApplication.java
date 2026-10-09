package gnubook.ppcore;

import java.util.concurrent.CountDownLatch;

import org.eclipse.equinox.app.IApplication;
import org.eclipse.equinox.app.IApplicationContext;
import org.osgi.framework.Bundle;
import org.osgi.framework.FrameworkUtil;

import name.abuchen.portfolio.PortfolioLog;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.online.Factory;

/**
 * Headless Eclipse application: runs Portfolio Performance's core (file format, PDF import, price feeds,
 * performance calculation) without its user interface and offers it as a small JSON API for gnubook.
 */
public class ServerApplication implements IApplication
{
    private final CountDownLatch stopped = new CountDownLatch(1);
    private HttpApi api;

    @Override
    public Object start(IApplicationContext context) throws Exception
    {
        Settings settings = Settings.load();
        // PP looks up its services (price feeds, exchange rates, search) with ServiceLoader and the context class
        // loader; they live in PP's core bundle, so that bundle's class loader must be the context class loader
        // before the first lookup (the results are cached in static fields)
        Thread.currentThread().setContextClassLoader(Client.class.getClassLoader());
        int feeds = Factory.getQuoteFeedProvider().size();
        Bundle pp = FrameworkUtil.getBundle(Client.class);
        String ppVersion = pp == null ? "?" : pp.getVersion().toString();
        QuoteUpdater.loadExchangeRates();
        api = new HttpApi(settings, ppVersion);
        api.start();
        context.applicationRunning();
        System.out.println("pp-core " + ppVersion + " listening on " + settings.bind + ":" + settings.port // NOSONAR
                        + " (" + feeds + " price sources)");
        Runtime.getRuntime().addShutdownHook(new Thread(stopped::countDown));
        stopped.await();
        return IApplication.EXIT_OK;
    }

    @Override
    public void stop()
    {
        try
        {
            if (api != null)
                api.stop();
        }
        catch (RuntimeException e)
        {
            PortfolioLog.error(e);
        }
        stopped.countDown();
    }
}
