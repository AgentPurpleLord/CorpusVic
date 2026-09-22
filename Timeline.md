# Timeline

The purpose of this document is to outline the 'legislative timeline' idea I have.

## Current Usage and Principal Acts

Currently a user can scroll through the Act and look at the various provisions. There is no method or way of reviewing how that provision may have changed over time. 

- There are principal Acts and Amending Acts.
- Principal Acts remain largely the same over time and are the focus of Corpus.
- A Principal Act is one where the title refers to its material, e.g. Criminal Procedure Act 2009, Evidence Act 2008, etc.


## Amendments, Repeals and Insertions

- Principal Acts have provisions amended, inserted or repealed via Amending Acts.
- The Amending Act often ceases to have effect shortly after enacting the changes to the Principal Act.
- Amendments can be made to _any_ part of a Principal Act (headings, notes, examples, provisions, schedules, etc.)
- Amendments are shown in the margin of the Principal Act in bold, small font.
- Amendments refer to an Amending Act via 'No. XX/YYYY'. This refers to what Act number and what year it was assented to (with act numbers increasing throughout the year as they are assented to).
- Text 'repealed' is what causes the '*   *   *   *' on a provision.

## The Timeline of a Provision

The purpose of the timeline function is to allow a user to pick a specific provision and review its wording changes or existence throughout history.

This changes the concept of a Principal Act from a document released in versions (as done on vic legislation) to a 'living' document. It changes and we need to keep track of those changes.

### The benefits

- A user can see previous offences which were repealed in order to determine if that offence existed at the time it was committed.
- A user can determine parliamentary intention in the clarification of words or phrases.
- Changes becoming readily apparent to a researcher, which is valuable (see Bail Act which is constantly changing).


### The challenges

- Difficult to represent this in a simple and comprehensible UI.
- Some provisions are wholly removed and replaced into other parts of the Act (see Crimes Act 1958 amendments).
- If a user is viewing the Principal Act (current version), where an entirely repealed section once existed, how would they be able to see that?


## The plan

### Public website 

- Where a provision has a past version, a chip button appears in the top right to enter 'historical mode' on that provision.
- Historical mode provides a horizontal timeline of the provision at that level of the provision, showing the most up to date on the right and previous versions on the left.
- A user can scroll through those previous versions.
- Only one previous version on the left and one future version on the right is ever shown at a time, scrolling through them shows the user that there are further changes.
- Information is placed at the top of the provision about when that version existed and what Act eventually amended it.
- Previous versions have an additional chip button to 'compare' the text with the 'current' text or with the next previous version / next future version.
- The comparison doesn't need to add further text, it merely highlights words that were added in green or words that were removed in red (with a strikethrough through those words).

### Review tool

- A Principal Act will always be parsed and reviewed at its 'current' format.
- When a future version of the Act is released, it can be added and parsed to the Principal Act.
- A comparison occurs between the two versions, where there are changes, only the new changes need to be reviewed. This lowers the burden of having to review an entire Act each version.
- The same occurs when a previous version is added to a Principal Act. The text is parsed, it is compared, any difference, the review can approve as being correct.
- UI will need to be added to add previous/newer versions of an Act. And to cycle through previous versions.
- There focus should always be on the 'most up to date' version of the Act. Only when provisions are different should they appear on the previous version review UI, to avoid clutter. 

