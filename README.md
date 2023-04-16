# ai4sqlite3
**Natural language query assistant for [SQLite databases](https://www.sqlite.org/index.html)**

Using a local SQLite3 database file, the command-line interface asks for your query intentions, uses OpenAI's ChatGPT API to formulate SQL fulfilling them, and then runs the SQL on your database. Bring your own [OpenAI API key](https://www.howtogeek.com/885918/how-to-get-an-openai-api-key/) ($ / free trial).

The tool sends your database *schema* and written query intentions to OpenAI. But NOT the result sets nor any other database *content*. The database is opened in read-only mode so that the AI cannot damage it.

### Quick start

<pre>
$ <b>export OPENAPI_API_KEY=xxx</b>
$ <b>pip3 install ai4sqlite3</b>
$ <b>wget https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite</b>
$ <b>ai4sqlite3 Chinook_Sqlite.sqlite --yes</b>
Analyzing schema of Chinook_Sqlite.sqlite in 4.9s 

This database models a digital music store. It includes tables for artists, albums,
tracks, genres, media types, invoices, customers, employees, playlists, and playlist
tracks. The tables are linked through foreign keys to form relationships, such as an
artist being associated with an album, an invoice being linked to a customer, and a
playlist being composed of multiple tracks. The database is designed to enable the store
to manage and track music sales, customer information, and employee records, as well as
organizing and categorizing the available music.

Please state the nature of the desired database query.
> <b>top five customer countries by 2011 revenue (round to cents)</b>

Generating SQL in 2.8s 

SELECT c.Country, ROUND(SUM(i.Total), 2) AS 'Revenue 2011'
FROM Customer c
JOIN Invoice i ON c.CustomerId = i.CustomerId
WHERE strftime('%Y', i.InvoiceDate) = '2011'
GROUP BY c.Country
ORDER BY SUM(i.Total) DESC
LIMIT 5;

Executing query in 0.1s 
+---------+--------------+
| Country | Revenue 2011 |
+---------+--------------+
|   USA   |    103.01    |
|  Canada |    55.44     |
| Germany |    48.57     |
|  France |    42.61     |
| Ireland |    32.75     |
+---------+--------------+

Next query?
> <b>percentage of all revenue from sales to North American customers</b>

Generating SQL in 3.3s 

SELECT 
    ROUND(SUM(i.Total) / (SELECT SUM(Total) FROM Invoice)*100, 2) AS "North American Revenue Percentage"
FROM 
    Invoice i
    INNER JOIN Customer c ON i.CustomerId = c.CustomerId
WHERE 
    c.Country = 'USA' OR c.Country = 'Canada';

Executing query in 0.1s 
+-----------------------------------+
| North American Revenue Percentage |
+-----------------------------------+
|               35.52               |
+-----------------------------------+

Next query?
>
</pre>

### Advice

* We reset the AI memory between `Next query?` prompts, so you can't implicitly refer back to prior queries.
* But you can use the up arrow key to recall a prior input to edit or add to.
* If SQLite rejects the AI's SQL, then we automatically request corrections (up to `--revisions` attempts).
* You can (usually) get general questions aswered with something like: *about the schema, what does each invoice line item refer to?*
* You might enjoy exploring your [Firefox](https://www.foxtonforensics.com/browser-history-examiner/firefox-history-location) or [Chrome](https://www.foxtonforensics.com/browser-history-examiner/chrome-history-location) browser history database (you might need to copy the file if your browser has it open).

### Challenging examples

Here are a few examples where gpt-3.5-turbo usually generates erroneous answers (but we show cherry-picked ones).

<pre>
> <b>Considering sales to USA customers, find the top-grossing artist in each state.</b>

Generating SQL in 13.4s 

WITH 
    -- Select only the sales to USA customers and
    -- join the necessary tables.
    usa_sales AS (
        SELECT il.*, c.State
        FROM InvoiceLine il
        INNER JOIN Invoice i ON il.InvoiceId=i.InvoiceId
        INNER JOIN Customer c ON i.CustomerId=c.CustomerId
        WHERE c.Country='USA'
    ),
 
    -- Calculate the total sale in dollars for each artist.
    artist_total_sales AS (
        SELECT ar.Name, SUM(usa_sales.UnitPrice*usa_sales.Quantity) AS total_sales
        FROM usa_sales
        INNER JOIN Track t ON usa_sales.TrackId=t.TrackId
        INNER JOIN Album al ON t.AlbumId=al.AlbumId
        INNER JOIN Artist ar ON al.ArtistId=ar.ArtistId
        GROUP BY ar.ArtistId
    ),
 
    -- Rank the artists by total sale in each state.
    -- If two or more artists share the top sale in a state,
    -- rank them alphabetically by artist name.
    ranked_artists AS (
        SELECT ar.Name, usa_sales.State,
               SUM(usa_sales.UnitPrice*usa_sales.Quantity) AS state_sales,
               RANK() OVER (
                    PARTITION BY usa_sales.State 
                    ORDER BY SUM(usa_sales.UnitPrice*usa_sales.Quantity) DESC, ar.Name
               ) AS rank_num
        FROM usa_sales
        INNER JOIN Track t ON usa_sales.TrackId=t.TrackId
        INNER JOIN Album al ON t.AlbumId=al.AlbumId
        INNER JOIN Artist ar ON al.ArtistId=ar.ArtistId
        GROUP BY ar.ArtistId, usa_sales.State
    )
 
-- Select only the top-ranked artist in each state.
SELECT State, Name AS Top_Grossing_Artist
FROM ranked_artists
WHERE rank_num=1
ORDER BY State;

Executing query in 0.1s 
+-------+--------------------------------+
| State |      Top_Grossing_Artist       |
+-------+--------------------------------+
|   AZ  |          Iron Maiden           |
|   CA  |          Iron Maiden           |
|   FL  |           Metallica            |
|   IL  |           The Office           |
|   MA  |       Milton Nascimento        |
|   NV  |          Cidade Negra          |
|   NY  |          Deep Purple           |
|   TX  |              Lost              |
|   UT  | Battlestar Galactica (Classic) |
|   WA  |           Metallica            |
|   WI  |           The Office           |
+-------+--------------------------------+

Next query?
> <b>Col 1: employee name. Col 2: subordinate employee name. Subordinates include transitive as well as direct reports.</b>

Generating SQL in 5.6s 

WITH RECURSIVE cte_subordinates AS (
  SELECT e1.FirstName || ' ' || e1.LastName AS EmployeeName, 
         e2.FirstName || ' ' || e2.LastName AS SubordinateName
  FROM Employee e1
  INNER JOIN Employee e2 ON e2.ReportsTo = e1.EmployeeId
  UNION
  SELECT cte.EmployeeName, e.FirstName || ' ' || e.LastName AS SubordinateName
  FROM cte_subordinates cte
  INNER JOIN Employee e ON e.ReportsTo = (
    SELECT EmployeeId FROM Employee WHERE FirstName || ' ' || LastName = cte.SubordinateName
  )
)
SELECT * FROM cte_subordinates ORDER BY EmployeeName, SubordinateName LIMIT 25;

Executing query in 0.1s 
+------------------+------------------+
|   EmployeeName   | SubordinateName  |
+------------------+------------------+
|   Andrew Adams   |   Jane Peacock   |
|   Andrew Adams   |  Laura Callahan  |
|   Andrew Adams   |  Margaret Park   |
|   Andrew Adams   | Michael Mitchell |
|   Andrew Adams   |  Nancy Edwards   |
|   Andrew Adams   |   Robert King    |
|   Andrew Adams   |  Steve Johnson   |
| Michael Mitchell |  Laura Callahan  |
| Michael Mitchell |   Robert King    |
|  Nancy Edwards   |   Jane Peacock   |
|  Nancy Edwards   |  Margaret Park   |
|  Nancy Edwards   |  Steve Johnson   |
+------------------+------------------+
</pre>
